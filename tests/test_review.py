from arb.db import Database


def test_review_queue_can_be_listed_filtered_and_resolved(tmp_path) -> None:
    db = Database(tmp_path / "review.db")
    try:
        db.add_review("sns", "https://shop/a", "AAA-1",
                      "low_match_confidence", {"confidence": 0.5})
        db.add_review("reede", "https://shop/b", "BBB-2",
                      "size_mapping_conflict", {"size": "8"})

        rows = db.list_reviews(reason="low_match_confidence")
        assert len(rows) == 1
        assert rows[0]["retailer"] == "sns"

        review_id = rows[0]["id"]
        assert db.resolve_review(review_id) is True
        assert db.resolve_review(review_id) is False
        assert db.list_reviews(reason="low_match_confidence") == []
        assert db.list_reviews(reason="low_match_confidence", resolved=True)[0][
            "id"] == review_id

        counts = {row["reason"]: row["n"] for row in db.review_reason_counts()}
        assert counts == {"size_mapping_conflict": 1}
    finally:
        db.close()
