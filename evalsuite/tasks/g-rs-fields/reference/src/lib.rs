pub fn parse_row(line: &str) -> Vec<String> {
    line.split(',').map(|f| f.trim().to_string()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plain() {
        assert_eq!(parse_row("a,b"), vec!["a", "b"]);
    }
}
