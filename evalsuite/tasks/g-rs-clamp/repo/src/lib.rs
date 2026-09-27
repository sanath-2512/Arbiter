/// Limit `v` to the inclusive range `lo..=hi`.
pub fn clamp(v: i64, lo: i64, hi: i64) -> i64 {
    if v < lo {
        lo
    } else if v > hi {
        lo
    } else {
        v
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn inside() {
        assert_eq!(clamp(5, 0, 10), 5);
    }
}
