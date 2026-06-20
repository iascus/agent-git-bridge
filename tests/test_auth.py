from fastapi import HTTPException

from renderer_server.auth import check_bearer_token


def test_check_bearer_token_accepts_matching_token() -> None:
    check_bearer_token("Bearer secret", "secret")


def test_check_bearer_token_rejects_wrong_token() -> None:
    try:
        check_bearer_token("Bearer nope", "secret")
    except HTTPException as exc:
        assert exc.status_code == 401
    else:
        raise AssertionError("expected HTTPException")
