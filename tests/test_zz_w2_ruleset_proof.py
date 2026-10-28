"""Epic 3995 W2.2 proof: a red test must block merge. Never merge this PR."""


def test_ruleset_blocks_red_ci():
    assert False, "deliberately red: proves the main ruleset blocks a failing PR"
