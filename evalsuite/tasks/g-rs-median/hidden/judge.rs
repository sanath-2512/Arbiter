use g_rs_median::*;

#[test]
fn judge_even() {
    assert_eq!(median(&[4.0, 1.0, 3.0, 2.0]), Some(2.5));
    assert_eq!(median(&[5.0, 1.0, 3.0]), Some(3.0));
    assert_eq!(median(&[]), None);
}
