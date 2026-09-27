pub fn fizzbuzz(n: u32) -> Vec<String> {
    (1..n)
        .map(|i| match (i % 3, i % 5) {
            (0, 0) => "FizzBuzz".to_string(),
            (0, _) => "Fizz".to_string(),
            (_, 0) => "Buzz".to_string(),
            _ => i.to_string(),
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fizz() {
        assert_eq!(fizzbuzz(4)[2], "Fizz");
    }
}
