use g_rs_unique::*;

#[test]
fn judge_order() {
    assert_eq!(unique(&[3, 1, 3, 2, 1]), vec![3, 1, 2]);
    assert_eq!(unique(&[]), Vec::<i32>::new());
}
