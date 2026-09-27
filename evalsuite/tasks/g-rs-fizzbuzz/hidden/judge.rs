use g_rs_fizzbuzz::*;

#[test]
fn judge_inclusive() {
    let v = fizzbuzz(15);
    assert_eq!(v.len(), 15);
    assert_eq!(v[14], "FizzBuzz");
    assert_eq!(fizzbuzz(1), vec!["1".to_string()]);
}
