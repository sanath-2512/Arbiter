pub fn median(values: &[f64]) -> Option<f64> {
    if values.is_empty() {
        return None;
    }
    let mut v = values.to_vec();
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let n = v.len();
    Some(v[n / 2])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn odd() {
        assert_eq!(median(&[3.0, 1.0, 2.0]), Some(2.0));
    }
}
