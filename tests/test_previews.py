import asyncio
import io
import threading
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import create_app
from app.projects import Projects


@pytest.mark.parametrize('size,orientation,expected', [
    ((3000, 2000), 1, (1920, 1280)),
    ((3000, 2000), 6, (1280, 1920)),
    ((6000, 4000), 6, (1280, 1920)),
    ((640, 480), 1, (640, 480)),
])
def test_high_resolution_preview_preserves_original_and_is_cached(tmp_path, size, orientation, expected):
    def capture(path, *_):
        exif = Image.Exif()
        exif[274] = orientation
        Image.new('RGB', size, '#31573c').save(path, 'JPEG', exif=exif)
    with TestClient(create_app(tmp_path, demo=True)) as client:
        with patch('app.camera.capture', side_effect=capture):
            frame_id = client.post('/api/capture', json={}).json()['id']
        original = (tmp_path / 'frames' / f'{frame_id}.jpg').read_bytes()
        path = tmp_path / 'previews' / f'{frame_id}.jpg'
        assert not path.exists()  # Old and new photos are prepared only when viewed.
        response = client.get(f'/media/previews/{frame_id}.jpg')
        assert response.status_code == 200
        assert response.headers['content-type'] == 'image/jpeg'
        with Image.open(io.BytesIO(response.content)) as image:
            assert image.size == expected
            assert image.getexif().get(274, 1) == 1
        assert (tmp_path / 'frames' / f'{frame_id}.jpg').read_bytes() == original
        stamp = path.stat().st_mtime_ns
        with patch('app.service.Image.open', side_effect=AssertionError('Decoded cached image')):
            assert client.get(f'/media/previews/{frame_id}.jpg').content == response.content
        assert path.stat().st_mtime_ns == stamp
        other = client.post('/api/projects', json={'name': 'Other'}).json()['id']
        assert client.get(f'/media/projects/{other}/previews/{frame_id}.jpg').status_code == 404
        assert client.get('/media/previews/not-a-frame.jpg').status_code == 404
        assert client.request('DELETE', f'/api/frames/{frame_id}', json={}).status_code == 200
        assert not path.exists()
        assert client.get(f'/media/previews/{frame_id}.jpg').status_code == 404


@pytest.mark.parametrize('delete_project', [False, True])
def test_deletion_waits_for_preview_generation_and_removes_cache(tmp_path, delete_project):
    async def scenario():
        manager = Projects(tmp_path, True)
        rec = manager.get('default')
        frame = await rec.take_photo()
        entered, release = threading.Event(), threading.Event()
        original = rec.preview_sync
        def blocked(frame_id):
            entered.set()
            assert release.wait(5)
            return original(frame_id)
        with patch.object(rec, 'preview_sync', side_effect=blocked):
            preview = asyncio.create_task(rec.preview_frame(frame['id']))
            assert await asyncio.to_thread(entered.wait, 5)
            deletion = asyncio.create_task(manager.delete('default') if delete_project else rec.delete_frame(frame['id']))
            try:
                await asyncio.sleep(.05)
                assert not deletion.done()
            finally:
                release.set()
            assert await preview
            await deletion
        assert not (tmp_path / 'previews' / f"{frame['id']}.jpg").exists()
        assert not (tmp_path / 'frames' / f"{frame['id']}.jpg").exists()
    asyncio.run(scenario())
