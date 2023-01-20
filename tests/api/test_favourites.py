import pytest


@pytest.mark.django_db
def test_favourites_flow(api_token, client):
    # Add a post
    response = client.post(
        "/api/v1/statuses",
        HTTP_AUTHORIZATION=f"Bearer {api_token.token}",
        HTTP_ACCEPT="application/json",
        content_type="application/json",
        data={
            "status": "Favourite test.",
            "visibility": "public",
        },
    ).json()
    assert response["content"] == "<p>Favourite test.</p>"

    status_id = response["id"]

    # Favourite it
    response = client.post(
        f"/api/v1/statuses/{status_id}/favourite",
        HTTP_AUTHORIZATION=f"Bearer {api_token.token}",
        HTTP_ACCEPT="application/json",
    ).json()
    assert response["favourited"] is True

    # Check if it's displaying at favourites endpoint
    response = client.get(
        "/api/v1/favourites",
        HTTP_AUTHORIZATION=f"Bearer {api_token.token}",
        HTTP_ACCEPT="application/json",
    ).json()
    assert response[0]["id"] == status_id