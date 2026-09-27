use g_rs_slug::*;

#[test]
fn judge_clean() {
    assert_eq!(slugify("Hello,  World!"), "hello-world");
    assert_eq!(slugify("  Rust 2024 -- edition "), "rust-2024-edition");
}
