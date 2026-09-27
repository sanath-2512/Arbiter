pub fn unique(values: &[i32]) -> Vec<i32> {
    let mut seen = std::collections::HashSet::new();
    values.iter().copied().filter(|x| seen.insert(*x)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn already_unique() {
        assert_eq!(unique(&[1, 2]), vec![1, 2]);
    }
}
