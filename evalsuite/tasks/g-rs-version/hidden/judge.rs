use g_rs_version::*;

#[test]
fn judge_numeric() {
    assert!(is_newer("1.10.0", "1.9.2"));
    assert!(!is_newer("1.2.0", "1.10.0"));
    assert!(is_newer("2.0.0", "1.99.99"));
}
