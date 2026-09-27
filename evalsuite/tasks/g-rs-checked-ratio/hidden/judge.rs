use g_rs_checked_ratio::*;

#[test]
fn judge_zero() {
    assert_eq!(ratio(1, 0), None);
    assert_eq!(ratio(9, 3), Some(3));
}
