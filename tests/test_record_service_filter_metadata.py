from ppbase.services.record_service import _filter_needs_relation_index


def test_relation_index_detection_ignores_literals_and_request_macros() -> None:
    assert not _filter_needs_relation_index(
        'expires_at < "2026-05-18T00:00:00.123Z"'
    )
    assert not _filter_needs_relation_index("owner = @request.auth.id")
    assert not _filter_needs_relation_index('title = "section.name"')
    assert _filter_needs_relation_index('owner.name = "Ada"')
    assert _filter_needs_relation_index("@collection.people.owner = id")


def test_relation_index_detection_skips_escaped_quotes_in_both_quote_styles() -> None:
    # pb.filter() escapes apostrophes as \' : the string does not end there.
    assert _filter_needs_relation_index(r"name = 'N\'Diaye' || team.name = 'Blue'")
    assert _filter_needs_relation_index(r'name = "Say \"hi" || team.name = "Blue"')
    assert not _filter_needs_relation_index(r"name = 'it\'s a.b'")
