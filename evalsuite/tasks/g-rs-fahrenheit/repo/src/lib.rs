pub fn c_to_f(c: f64) -> f64 {
    (c as i64 * 9 / 5 + 32) as f64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn boiling() {
        assert_eq!(c_to_f(100.0), 212.0);
    }
}
