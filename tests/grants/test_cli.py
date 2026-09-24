from agents.grants.cli import main


def test_dry_run_exits_zero_and_prints_report(capsys):
    exit_code = main(["--dry-run"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Reject reasons:" in captured.out
    assert "Top" in captured.out


def test_missing_dry_run_flag_is_an_error():
    try:
        main([])
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError("expected SystemExit for missing --dry-run")
