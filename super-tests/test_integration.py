"""
Integration tests for pykrozen with the new RadixRouter.

Tests the full framework including:
- Route parameter extraction via decorators
- Wildcard routes
- Request.params attribute
- Backwards compatibility with existing routes
"""

import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Import fresh pykrozen module
import importlib
import pykrozen
importlib.reload(pykrozen)

from pykrozen import App, Request, get, post, put, delete, patch


def test_app_routing_with_params():
    """Test App class routing with dynamic parameters."""
    app = App()

    results = []

    @app.get("/users")
    def list_users(req):
        results.append(("list_users", req.params))
        return {"body": {"users": []}}

    @app.get("/users/:user_id")
    def get_user(req):
        results.append(("get_user", dict(req.params)))
        return {"body": {"id": req.params["user_id"]}}

    @app.get("/users/:user_id/posts/:post_id")
    def get_user_post(req):
        results.append(("get_user_post", dict(req.params)))
        return {"body": req.params}

    @app.post("/users")
    def create_user(req):
        results.append(("create_user", dict(req.params)))
        return {"body": {"created": True}, "status": 201}

    @app.put("/users/:user_id")
    def update_user(req):
        results.append(("update_user", dict(req.params)))
        return {"body": {"updated": req.params["user_id"]}}

    @app.delete("/users/:user_id")
    def delete_user(req):
        results.append(("delete_user", dict(req.params)))
        return {"body": {"deleted": req.params["user_id"]}}

    # Test routing
    req = Request(method="GET", path="/users")
    result = app.route("GET", "/users", req)
    assert result is not None
    assert ("list_users", {}) in results

    results.clear()
    req = Request(method="GET", path="/users/42")
    result = app.route("GET", "/users/42", req)
    assert result is not None
    assert result["body"] == {"id": "42"}
    assert ("get_user", {"user_id": "42"}) in results

    results.clear()
    req = Request(method="GET", path="/users/10/posts/5")
    result = app.route("GET", "/users/10/posts/5", req)
    assert result is not None
    assert result["body"] == {"user_id": "10", "post_id": "5"}
    assert ("get_user_post", {"user_id": "10", "post_id": "5"}) in results

    results.clear()
    req = Request(method="POST", path="/users")
    result = app.route("POST", "/users", req)
    assert result is not None
    assert result["status"] == 201

    results.clear()
    req = Request(method="PUT", path="/users/99")
    result = app.route("PUT", "/users/99", req)
    assert result is not None
    assert result["body"]["updated"] == "99"

    results.clear()
    req = Request(method="DELETE", path="/users/77")
    result = app.route("DELETE", "/users/77", req)
    assert result is not None
    assert result["body"]["deleted"] == "77"

    print("[PASS] App routing with params test passed")


def test_wildcard_routes():
    """Test wildcard routes in the App."""
    app = App()

    @app.get("/files/*filepath")
    def serve_files(req):
        return {"body": {"file": req.params.get("filepath", "")}}

    @app.get("/static/*")
    def serve_static(req):
        return {"body": {"static": True}}

    # Test wildcard
    req = Request(method="GET", path="/files/docs/readme.md")
    result = app.route("GET", "/files/docs/readme.md", req)
    assert result is not None
    assert result["body"] == {"file": "docs/readme.md"}

    req = Request(method="GET", path="/files/a/b/c/d.txt")
    result = app.route("GET", "/files/a/b/c/d.txt", req)
    assert result is not None
    assert result["body"] == {"file": "a/b/c/d.txt"}

    print("[PASS] Wildcard routes test passed")


def test_static_priority_over_dynamic():
    """Test that static routes have priority over dynamic."""
    app = App()

    @app.get("/users/list")
    def list_users(req):
        return {"body": {"type": "static"}}

    @app.get("/users/:id")
    def get_user(req):
        return {"body": {"type": "dynamic", "id": req.params["id"]}}

    # Static should match first
    req = Request(method="GET", path="/users/list")
    result = app.route("GET", "/users/list", req)
    assert result is not None
    assert result["body"]["type"] == "static"

    # Dynamic should match for other paths
    req = Request(method="GET", path="/users/123")
    result = app.route("GET", "/users/123", req)
    assert result is not None
    assert result["body"]["type"] == "dynamic"
    assert result["body"]["id"] == "123"

    print("[PASS] Static priority over dynamic test passed")


def test_request_params_attribute():
    """Test that Request.params is correctly populated."""
    app = App()

    captured_params = []

    @app.get("/api/:version/resource/:id")
    def handler(req):
        captured_params.append(dict(req.params))
        return {"body": "ok"}

    req = Request(method="GET", path="/api/v2/resource/abc123")
    result = app.route("GET", "/api/v2/resource/abc123", req)
    assert result is not None
    assert len(captured_params) == 1
    assert captured_params[0] == {"version": "v2", "id": "abc123"}

    print("[PASS] Request.params attribute test passed")


def test_backwards_compatibility():
    """Test that existing simple routes still work."""
    app = App()

    @app.get("/health")
    def health(req):
        return {"body": {"status": "ok"}}

    @app.get("/")
    def root(req):
        return {"body": {"message": "Hello"}}

    @app.post("/data")
    def post_data(req):
        return {"body": {"received": req.body}, "status": 201}

    # Test simple routes
    req = Request(method="GET", path="/health")
    result = app.route("GET", "/health", req)
    assert result is not None
    assert result["body"]["status"] == "ok"

    req = Request(method="GET", path="/")
    result = app.route("GET", "/", req)
    assert result is not None
    assert result["body"]["message"] == "Hello"

    req = Request(method="POST", path="/data", body={"key": "value"})
    result = app.route("POST", "/data", req)
    assert result is not None
    assert result["status"] == 201

    print("[PASS] Backwards compatibility test passed")


def test_patch_method():
    """Test PATCH method routing."""
    app = App()

    @app.patch("/users/:id")
    def patch_user(req):
        return {"body": {"patched": req.params["id"]}}

    req = Request(method="PATCH", path="/users/55")
    result = app.route("PATCH", "/users/55", req)
    assert result is not None
    assert result["body"] == {"patched": "55"}

    print("[PASS] PATCH method test passed")


def test_multiple_methods_same_path():
    """Test different methods on the same path."""
    app = App()

    @app.get("/resource/:id")
    def get_resource(req):
        return {"body": {"method": "GET", "id": req.params["id"]}}

    @app.put("/resource/:id")
    def put_resource(req):
        return {"body": {"method": "PUT", "id": req.params["id"]}}

    @app.delete("/resource/:id")
    def delete_resource(req):
        return {"body": {"method": "DELETE", "id": req.params["id"]}}

    # All methods should work on the same path
    for method in ["GET", "PUT", "DELETE"]:
        req = Request(method=method, path="/resource/123")
        result = app.route(method, "/resource/123", req)
        assert result is not None
        assert result["body"]["method"] == method
        assert result["body"]["id"] == "123"

    print("[PASS] Multiple methods same path test passed")


def run_all_tests():
    """Run all integration tests."""
    print("\n" + "=" * 60)
    print("PyKrozen Integration Test Suite")
    print("=" * 60 + "\n")

    test_app_routing_with_params()
    test_wildcard_routes()
    test_static_priority_over_dynamic()
    test_request_params_attribute()
    test_backwards_compatibility()
    test_patch_method()
    test_multiple_methods_same_path()

    print("\n" + "=" * 60)
    print("All integration tests passed!")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    run_all_tests()
