pub fn word_count(text: &str) -> usize {
    if text.is_empty() {
        return 0;
    }
    text.split(' ').count()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn simple() {
        assert_eq!(word_count("hello world"), 2);
    }
}
