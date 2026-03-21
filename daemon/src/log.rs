use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use memmap2::Mmap;

use crate::types::LogEvent;

/// Header: [u64 timestamp LE][u32 length LE][msgpack payload]
const HEADER_SIZE: usize = 8 + 4; // 12 bytes

/// Index entry: [u32 step LE][u64 offset LE]
const INDEX_ENTRY_SIZE: usize = 4 + 8; // 12 bytes

pub struct EventLog {
    log_path: PathBuf,
    idx_path: PathBuf,
    log_file: File,
    idx_file: File,
}

impl EventLog {
    pub fn open(run_id: &str) -> io::Result<Self> {
        let base_dir = dirs_path();
        fs::create_dir_all(&base_dir)?;

        let log_path = base_dir.join(format!("{}.log", run_id));
        let idx_path = base_dir.join(format!("{}.idx", run_id));

        let log_file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)?;

        let idx_file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&idx_path)?;

        Ok(Self {
            log_path,
            idx_path,
            log_file,
            idx_file,
        })
    }

    pub fn open_at(log_path: PathBuf, idx_path: PathBuf) -> io::Result<Self> {
        let parent = log_path.parent().unwrap_or(Path::new("."));
        fs::create_dir_all(parent)?;

        let log_file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)?;

        let idx_file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&idx_path)?;

        Ok(Self {
            log_path,
            idx_path,
            log_file,
            idx_file,
        })
    }

    /// Append an event to the log. If the event is an Observe, also write an index entry.
    pub fn append(&mut self, event: &LogEvent, timestamp: u64) -> io::Result<u64> {
        let offset = self.log_file.metadata()?.len();
        let payload = rmp_serde::to_vec(event)
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
        let length = payload.len() as u32;

        // Write header + payload
        self.log_file.write_all(&timestamp.to_le_bytes())?;
        self.log_file.write_all(&length.to_le_bytes())?;
        self.log_file.write_all(&payload)?;
        self.log_file.flush()?;
        sync_file(&self.log_file)?;

        // Write index entry for observations only
        if let LogEvent::Observe(obs) = event {
            self.idx_file.write_all(&obs.step.to_le_bytes())?;
            self.idx_file.write_all(&offset.to_le_bytes())?;
            self.idx_file.flush()?;
            sync_file(&self.idx_file)?;
        }

        Ok(offset)
    }

    /// Read all events from the log file by memory-mapping it.
    pub fn read_all(&self) -> io::Result<Vec<(u64, LogEvent)>> {
        self.read_from(0)
    }

    /// Read events starting from a byte offset.
    pub fn read_from(&self, start_offset: u64) -> io::Result<Vec<(u64, LogEvent)>> {
        let file = File::open(&self.log_path)?;
        let file_len = file.metadata()?.len();
        if file_len == 0 {
            return Ok(Vec::new());
        }

        let mmap = unsafe { Mmap::map(&file)? };
        let mut pos = start_offset as usize;
        let mut events = Vec::new();

        while pos + HEADER_SIZE <= mmap.len() {
            let timestamp = u64::from_le_bytes(
                mmap[pos..pos + 8].try_into().unwrap(),
            );
            let length = u32::from_le_bytes(
                mmap[pos + 8..pos + 12].try_into().unwrap(),
            ) as usize;
            pos += HEADER_SIZE;

            if pos + length > mmap.len() {
                return Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "corrupt log: entry extends past end of file",
                ));
            }

            let event: LogEvent = rmp_serde::from_slice(&mmap[pos..pos + length])
                .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
            events.push((timestamp, event));
            pos += length;
        }

        Ok(events)
    }

    /// Read the index file and return (step, offset) pairs.
    pub fn read_index(&self) -> io::Result<Vec<(u32, u64)>> {
        let file = match File::open(&self.idx_path) {
            Ok(f) => f,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(e) => return Err(e),
        };
        let file_len = file.metadata()?.len();
        if file_len == 0 {
            return Ok(Vec::new());
        }

        let mmap = unsafe { Mmap::map(&file)? };
        let mut pos = 0;
        let mut entries = Vec::new();

        while pos + INDEX_ENTRY_SIZE <= mmap.len() {
            let step = u32::from_le_bytes(mmap[pos..pos + 4].try_into().unwrap());
            let offset = u64::from_le_bytes(mmap[pos + 4..pos + 12].try_into().unwrap());
            entries.push((step, offset));
            pos += INDEX_ENTRY_SIZE;
        }

        Ok(entries)
    }

    /// Check if the log contains a run_end event.
    pub fn has_run_end(&self) -> io::Result<bool> {
        let events = self.read_all()?;
        Ok(events.iter().any(|(_, e)| matches!(e, LogEvent::RunEnd { .. })))
    }

    pub fn log_path(&self) -> &Path {
        &self.log_path
    }

    pub fn idx_path(&self) -> &Path {
        &self.idx_path
    }

    pub fn log_size(&self) -> io::Result<u64> {
        Ok(self.log_file.metadata()?.len())
    }
}

fn dirs_path() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    PathBuf::from(home).join(".crux").join("runs")
}

#[cfg(target_family = "unix")]
fn sync_file(file: &File) -> io::Result<()> {
    file.sync_data()
}

#[cfg(not(target_family = "unix"))]
fn sync_file(file: &File) -> io::Result<()> {
    file.sync_data()
}
