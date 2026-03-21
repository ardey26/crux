use rand::Rng;
use std::time::{SystemTime, UNIX_EPOCH};

const CROCKFORD_BASE32: &[u8; 32] = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";

/// Generate a ULID: 48-bit ms timestamp + 80-bit random, Crockford base32 encoded (26 chars).
pub fn new_ulid() -> String {
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64;
    let mut rng = rand::thread_rng();
    let random_hi: u16 = rng.gen();
    let random_lo: u64 = rng.gen();
    ulid_from_parts(timestamp, random_hi, random_lo)
}

fn ulid_from_parts(timestamp: u64, random_hi: u16, random_lo: u64) -> String {
    // ULID is 128 bits: 48-bit timestamp (10 chars) + 80-bit random (16 chars)
    let mut chars = [0u8; 26];

    // Encode timestamp (48 bits) into 10 base32 characters, MSB first
    let ts = timestamp & 0xFFFF_FFFF_FFFF; // mask to 48 bits
    for i in 0..10 {
        let shift = (9 - i) * 5;
        let idx = if shift < 64 {
            ((ts >> shift) & 0x1F) as usize
        } else {
            0
        };
        chars[i] = CROCKFORD_BASE32[idx];
    }

    // Combine random_hi (16 bits) + random_lo (64 bits) = 80 bits
    // Encode into 16 base32 characters
    let random: u128 = ((random_hi as u128) << 64) | (random_lo as u128);
    for i in 0..16 {
        let shift = (15 - i) * 5;
        let idx = ((random >> shift) & 0x1F) as usize;
        chars[10 + i] = CROCKFORD_BASE32[idx];
    }

    String::from_utf8(chars.to_vec()).unwrap()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ulid_is_26_chars() {
        let id = new_ulid();
        assert_eq!(id.len(), 26);
        assert!(id.chars().all(|c| CROCKFORD_BASE32.contains(&(c as u8))));
    }

    #[test]
    fn ulids_are_unique() {
        let a = new_ulid();
        let b = new_ulid();
        assert_ne!(a, b);
    }
}
