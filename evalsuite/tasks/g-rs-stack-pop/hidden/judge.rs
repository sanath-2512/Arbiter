use g_rs_stack_pop::*;

#[test]
fn judge_empty_pop() {
    let mut s: Stack<i32> = Stack::new();
    assert_eq!(s.pop(), None);
    s.push(1);
    assert_eq!(s.pop(), Some(1));
    assert_eq!(s.pop(), None);
}
