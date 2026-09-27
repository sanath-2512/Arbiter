use g_rs_leap::*;

#[test]
fn judge_400() {
    assert!(is_leap(2000));
    assert!(!is_leap(1900));
    assert!(is_leap(2024));
}
