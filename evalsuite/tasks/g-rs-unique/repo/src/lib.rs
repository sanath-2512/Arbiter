pub fn unique(values: &[i32]) -> Vec<i32> {
    let mut v = values.to_vec();
    v.sort();
    v.dedup();
    v
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn already_unique() {
        assert_eq!(unique(&[1, 2]), vec![1, 2]);
    }
}
