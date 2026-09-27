use g_rs_word_count::*;

#[test]
fn judge_whitespace() {
    assert_eq!(word_count("a  b"), 2);
    assert_eq!(word_count("  one\ttwo\nthree  "), 3);
    assert_eq!(word_count("   "), 0);
}
