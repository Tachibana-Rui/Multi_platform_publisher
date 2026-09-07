import atexit
import io
import os
from pathlib import Path
import tempfile

from fastapi.testclient import TestClient
from PIL import Image, ImageOps
import pytest


@pytest.fixture(scope="module")
def client():
    # Keep standalone runs isolated from the user's library as well as full runs.
    with tempfile.TemporaryDirectory() as folder:
        if "CONTENT_HUB_DATABASE_URL" not in os.environ:
            os.environ["CONTENT_HUB_DATABASE_URL"] = f"sqlite:///{Path(folder).as_posix()}/test.db"
            os.environ["CONTENT_HUB_DATA_DIR"] = f"{folder}/data"
            os.environ["CONTENT_HUB_UPLOAD_DIR"] = f"{folder}/uploads"
        from app.main import app
        from app.database import engine
        atexit.register(engine.dispose)
        with TestClient(app) as test_client:
            yield test_client
        # The application's engine is process-global; defer removing its files.
        engine.dispose()


def image_bytes(size=(101, 80), *, orientation=None):
    image = Image.new("RGB", size)
    image.putdata([(x % 256, y % 256, (x + y) % 256) for y in range(size[1]) for x in range(size[0])])
    output = io.BytesIO()
    exif = Image.Exif()
    if orientation:
        exif[274] = orientation
    image.save(output, "PNG", exif=exif)
    return output.getvalue()


def create_post(client, pictures):
    post = client.post("/api/posts", json={"title": "切图", "body": "原稿"}).json()
    response = client.post(f"/api/posts/{post['id']}/assets", files=[
        ("files", (f"scene-{index}.png", picture, "image/png"))
        for index, picture in enumerate(pictures)
    ])
    assert response.status_code == 201, response.text
    return response.json()


def split(client, post, selected, sources, platform="douyin"):
    return client.post(f"/api/posts/{post['id']}/platform-versions/{platform}/split-landscapes", json={
        "selected_asset_ids": selected, "split_asset_ids": sources,
        "title": "未保存的标题", "body": "保留当前编辑内容 #cosplay",
    })


def test_split_preserves_pixels_order_original_and_publication_snapshot(client, monkeypatch):
    from app.publish_agent import publication_agent
    from app.media_storage import migrate_media_layout
    monkeypatch.setattr(publication_agent, "start", lambda _id: True)
    raw = image_bytes()
    post = create_post(client, [raw, image_bytes((40, 60))])
    source, portrait = [asset["id"] for asset in post["assets"]]
    other_url = f"/api/posts/{post['id']}/platform-versions/xiaohongshu"
    other_before = client.get(other_url).json()["selected_asset_ids"]
    # A pre-existing task must retain its original immutable asset list.
    old_task = client.post("/api/publications", json={"post_id": post["id"], "platform": "douyin"}).json()
    response = split(client, post, [portrait, source], [source])
    assert response.status_code == 200, response.text
    version = response.json()
    portrait_id, left, right, original = version["selected_asset_ids"]
    assert [portrait_id, original] == [portrait, source]
    assert version["title"] == "未保存的标题"
    assert version["body"] == "保留当前编辑内容 #cosplay"
    assets = {asset["id"]: asset for asset in version["assets"]}
    assert client.get(assets[source]["url"]).content == raw
    with Image.open(io.BytesIO(raw)) as source_image:
        for asset_id, box in [(left, (0, 0, 50, 80)), (right, (50, 0, 101, 80))]:
            crop = Image.open(io.BytesIO(client.get(assets[asset_id]["url"]).content))
            assert crop.size == source_image.crop(box).size
            assert crop.tobytes() == source_image.crop(box).tobytes()
            assert crop.width < crop.height
    assert client.get(other_url).json()["selected_asset_ids"] == other_before
    assert client.get(f"/api/publications/{old_task['id']}").json()["asset_ids"] == old_task["asset_ids"]
    # Reopening / restarting and clicking again reuses the same two crop records.
    migrate_media_layout()
    again = split(client, post, version["selected_asset_ids"], [source]).json()
    assert again["selected_asset_ids"] == version["selected_asset_ids"]
    assert len(again["assets"]) == 4
    snapshot = publication_agent._build_snapshot(old_task["id"])
    assert [asset.id for asset in snapshot.assets] == old_task["asset_ids"]
    # End the old pending attempt before starting a new one with the edited draft.
    client.post(f"/api/publications/{old_task['id']}/cancel")
    task = client.post("/api/publications", json={"post_id": post["id"], "platform": "douyin"}).json()
    snapshot = publication_agent._build_snapshot(task["id"])
    assert [asset.id for asset in snapshot.assets] == [portrait, left, right, source]


def test_batch_split_keeps_each_triplet_in_selected_order(client):
    post = create_post(client, [image_bytes(), image_bytes((110, 90))])
    first, second = [asset["id"] for asset in post["assets"]]
    result = split(client, post, [second, first], [first, second]).json()
    assert result["selected_asset_ids"][2::3] == [second, first]
    assert len(set(result["selected_asset_ids"])) == 6


@pytest.mark.parametrize("size,orientation", [((41, 63), 6), ((240, 50), None)])
def test_split_honors_orientation_and_pads_ultrawide_without_cutting_pixels(client, size, orientation):
    raw = image_bytes(size, orientation=orientation)
    post = create_post(client, [raw])
    source = post["assets"][0]["id"]
    assert post["assets"][0]["width"] > post["assets"][0]["height"]
    response = split(client, post, [source], [source])
    assert response.status_code == 200, response.text
    result = response.json()
    assets = {asset["id"]: asset for asset in result["assets"]}
    oriented = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
    left_width = oriented.width // 2
    for index, part_id in enumerate(result["selected_asset_ids"][:2]):
        crop = Image.open(io.BytesIO(client.get(assets[part_id]["url"]).content))
        assert crop.width < crop.height
        y = (crop.height - oriented.height) // 2
        box = (0, 0, left_width, oriented.height) if index == 0 else (left_width, 0, oriented.width, oriented.height)
        assert crop.crop((0, y, crop.width, y + oriented.height)).tobytes() == oriented.crop(box).tobytes()


def test_split_uses_matched_high_resolution_original(client):
    from app.config import settings
    from app.database import SessionLocal
    from app.models import AssetMatch
    post = create_post(client, [image_bytes((60, 40))])
    source = post["assets"][0]["id"]
    name = f"{post['id']}/originals/high-resolution.png"
    (settings.upload_dir / name).write_bytes(image_bytes((300, 200)))
    with SessionLocal() as db:
        db.add(AssetMatch(downloaded_asset_id=source, status="matched", copied_storage_name=name))
        db.commit()
    result = split(client, post, [source], [source]).json()
    assets = {asset["id"]: asset for asset in result["assets"]}
    assert [(assets[aid]["width"], assets[aid]["height"]) for aid in result["selected_asset_ids"][:2]] == [(150, 200), (150, 200)]


def test_invalid_batch_rolls_back_files_and_draft(client):
    from app.config import settings
    post = create_post(client, [image_bytes(), image_bytes((20, 30))])
    ids = [asset["id"] for asset in post["assets"]]
    url = f"/api/posts/{post['id']}/platform-versions/douyin"
    before = client.get(url).json()
    response = split(client, post, ids, ids)
    assert response.status_code == 422
    assert client.get(url).json() == before
    assert not list((settings.upload_dir / post["id"] / "processed").glob("*.png"))
    outsider = create_post(client, [image_bytes()])["assets"][0]["id"]
    assert split(client, post, ids, [outsider]).status_code == 422
    assert split(client, post, [ids[1]], [ids[0]]).status_code == 422
    assert split(client, post, [ids[0], ids[0]], [ids[0]]).status_code == 422


def test_split_respects_platform_image_limit_and_cleans_created_files(client):
    from app.config import settings
    post = create_post(client, [image_bytes()] + [image_bytes((20, 30))] * 7)
    ids = [asset["id"] for asset in post["assets"]]
    response = split(client, post, ids, [ids[0]], platform="bilibili")
    assert response.status_code == 422
    assert "最多选择 9" in response.json()["detail"]
    assert len(client.get(f"/api/posts/{post['id']}").json()["assets"]) == 8
    assert not list((settings.upload_dir / post["id"] / "processed").glob("*.png"))


def test_split_button_preview_and_saved_order_in_browser(client, tmp_path):
    from urllib.parse import urlsplit
    from playwright.sync_api import expect, sync_playwright
    from app.publishers.browser import get_browser_executable

    post = create_post(client, [image_bytes((601, 450))])
    source = post["assets"][0]["id"]
    with sync_playwright() as playwright:
        executable = get_browser_executable()
        if executable is None and not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("Install Chromium or Edge to run browser UI checks")
        browser = playwright.chromium.launch(headless=True, **({"executable_path": str(executable)} if executable else {}))
        page = browser.new_page(viewport={"width": 1360, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            request = route.request
            url = urlsplit(request.url)
            response = client.request(
                request.method, url.path + (f"?{url.query}" if url.query else ""),
                content=request.post_data_buffer,
                headers={key: value for key, value in request.headers.items() if key.lower() in {"content-type", "accept"}},
            )
            route.fulfill(status=response.status_code, body=response.content,
                          headers={"content-type": response.headers.get("content-type", "application/octet-stream")})

        page.route("http://content-hub.test/**", serve)
        page.goto("http://content-hub.test/")
        page.locator(f'[data-post-id="{post["id"]}"]').click()
        page.locator('#openPlatformButton').click()
        button = page.locator('#splitLandscapeButton')
        expect(button).to_be_enabled()
        page.locator('#platformTitle').fill('切图时保留此标题')
        page.locator('#generationPrompt').fill('我的自定义提示词')
        button.click()
        expect(button).to_have_text('横图切成两张竖图')
        expect(page.locator('#uploadOrderStrip .upload-order-card')).to_have_count(3)
        expect(page.locator('#platformTitle')).to_have_value('切图时保留此标题')
        expect(page.locator('#generationPrompt')).to_have_value('我的自定义提示词')
        expect(page.locator('#selectedImageCount')).to_have_text('已选择 3 张图片')
        captions = page.locator('#uploadOrderStrip figcaption').all_text_contents()
        assert '左半图' in captions[0] and '右半图' in captions[1]
        assert 'scene-0.png' in captions[2]
        ids = page.locator('#platformAssetsGrid input:checked').evaluate_all('inputs => inputs.map(input => input.value)')
        assert ids[-1] == source
        page.locator('#savePlatformButton').click()
        expect(page.locator('#savePlatformButton')).to_have_text('保存预填草稿')
        saved = client.get(f"/api/posts/{post['id']}/platform-versions/douyin").json()
        assert saved['selected_asset_ids'] == ids
        page.locator('#uploadOrderStrip button').first.click()
        expect(page.locator('#imageViewer')).to_be_visible()
        page.locator('#closeImageViewerButton').click()
        page.screenshot(path=str(tmp_path / 'landscape-split-desktop.png'))
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator('#splitLandscapeButton').scroll_into_view_if_needed()
        assert page.locator('#uploadOrderStrip').evaluate('el => el.scrollWidth > el.clientWidth')
        page.locator('#uploadOrderStrip').evaluate('el => el.scrollLeft = el.scrollWidth')
        assert page.locator('#uploadOrderStrip').evaluate('el => el.scrollLeft > 0')
        page.screenshot(path=str(tmp_path / 'landscape-split-mobile.png'))
        assert not errors
        browser.close()
