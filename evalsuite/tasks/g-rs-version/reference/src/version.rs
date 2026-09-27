/// True when version `a` is newer than version `b` (dotted numbers).
pub fn is_newer(a: &str, b: &str) -> bool {
    let parse = |s: &str| s.split('.').map(|p| p.parse::<u64>().unwrap_or(0)).collect::<Vec<_>>();
    parse(a) > parse(b)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn simple() {
        assert!(is_newer("1.3.0", "1.2.0"));
    }
}
