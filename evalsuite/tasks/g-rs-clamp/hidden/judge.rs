use g_rs_clamp::*;

#[test]
fn judge_above() {
    assert_eq!(clamp(15, 0, 10), 10);
    assert_eq!(clamp(-3, 0, 10), 0);
    assert_eq!(clamp(7, 0, 10), 7);
}
