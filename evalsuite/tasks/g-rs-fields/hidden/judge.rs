use g_rs_fields::*;

#[test]
fn judge_trim() {
    assert_eq!(parse_row("a, b ,c"), vec!["a", "b", "c"]);
    assert_eq!(parse_row("  x  "), vec!["x"]);
}
