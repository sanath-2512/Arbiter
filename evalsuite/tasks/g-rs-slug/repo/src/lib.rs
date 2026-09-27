pub fn slugify(s: &str) -> String {
    s.to_lowercase().replace(' ', "-")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn simple() {
        assert_eq!(slugify("a b"), "a-b");
    }
}
