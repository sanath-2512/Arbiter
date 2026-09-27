/// Parse durations like "2h", "45m", "1h30m", "10s" into seconds.
pub fn parse_duration(s: &str) -> Option<u64> {
    let mut total = 0u64;
    let mut num = String::new();
    for c in s.chars() {
        if c.is_ascii_digit() {
            num.push(c);
            continue;
        }
        let n: u64 = num.parse().ok()?;
        num.clear();
        total += match c {
            'h' => n * 3600,
            'm' => n * 60,
            's' => n,
            _ => return None,
        };
    }
    if !num.is_empty() {
        return None;
    }
    Some(total)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hours() {
        assert_eq!(parse_duration("2h"), Some(7200));
    }
}
