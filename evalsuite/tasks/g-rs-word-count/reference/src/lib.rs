pub fn word_count(text: &str) -> usize {
    text.split_whitespace().count()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn simple() {
        assert_eq!(word_count("hello world"), 2);
    }
}
