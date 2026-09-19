from ppbase.services.record_service import _filter_needs_relation_index


def test_relation_index_detection_ignores_literals_and_request_macros() -> None:
    assert not _filter_needs_relation_index(
        'expires_at < "2026-05-18T00:00:00.123Z"'
    )
    assert not _filter_needs_relation_index("owner = @request.auth.id")
    assert not _filter_needs_relation_index('title = "section.name"')
    assert _filter_needs_relation_index('owner.name = "Ada"')
    assert _filter_needs_relation_index("@collection.people.owner = id")
