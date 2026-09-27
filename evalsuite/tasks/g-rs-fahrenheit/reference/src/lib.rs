pub fn c_to_f(c: f64) -> f64 {
    c * 9.0 / 5.0 + 32.0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn boiling() {
        assert_eq!(c_to_f(100.0), 212.0);
    }
}
