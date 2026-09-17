def test_brand_page_is_public_and_supports_head(client):
    response = client.get("/about")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Кэпик" in response.text
    assert "https://t.me/kkepik_bot" in response.text
    assert "Навык готовится к публикации" in response.text
    assert "<script" not in response.text
    head = client.head("/about")
    assert head.status_code == 200 and not head.content


def test_webmaster_proof_is_public(client):
    result = client.get("/yandex_f031293cd6d4c038.html")
    assert result.status_code == 200
    assert "Verification: f031293cd6d4c038" in result.text
