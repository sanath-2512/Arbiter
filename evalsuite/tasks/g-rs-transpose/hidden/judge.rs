use g_rs_transpose::*;

#[test]
fn judge_rect() {
    assert_eq!(transpose(&[vec![1, 2, 3], vec![4, 5, 6]]), vec![vec![1, 4], vec![2, 5], vec![3, 6]]);
}
