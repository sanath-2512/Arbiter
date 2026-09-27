pub fn transpose(m: &[Vec<i32>]) -> Vec<Vec<i32>> {
    if m.is_empty() {
        return vec![];
    }
    let rows = m.len();
    let cols = m[0].len();
    (0..cols).map(|c| (0..rows).map(|r| m[r][c]).collect()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn square() {
        assert_eq!(transpose(&[vec![1, 2], vec![3, 4]]), vec![vec![1, 3], vec![2, 4]]);
    }
}
