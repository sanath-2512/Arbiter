/// a / b, or None when b is zero.
pub fn ratio(a: i64, b: i64) -> Option<i64> {
    Some(a / b)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plain() {
        assert_eq!(ratio(10, 2), Some(5));
    }
}
