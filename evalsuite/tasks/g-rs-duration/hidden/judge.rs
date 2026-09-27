use g_rs_duration::*;

#[test]
fn judge_minutes() {
    assert_eq!(parse_duration("1h30m"), Some(5400));
    assert_eq!(parse_duration("45m"), Some(2700));
    assert_eq!(parse_duration("2h5m10s"), Some(7510));
}
