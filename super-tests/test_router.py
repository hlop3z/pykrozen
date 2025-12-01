"""
Tests for the RadixRouter implementation.

Run with: python -m pytest test_router.py -v
Or just: python test_router.py
"""

import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pykrozen.router import RadixRouter, RouteMatch, NODE_STATIC, NODE_DYNAMIC, NODE_WILDCARD


def test_static_routes():
    """Test basic static route matching."""
    router = RadixRouter()

    def handler_root(): return "root"
    def handler_users(): return "users"
    def handler_posts(): return "posts"

    router.add("GET", "/", handler_root)
    router.add("GET", "/users", handler_users)
    router.add("GET", "/posts", handler_posts)
    router.compile()

    # Test matches
    match = router.match("GET", "/")
    assert match.matched
    assert match.handler() == "root"

    match = router.match("GET", "/users")
    assert match.matched
    assert match.handler() == "users"

    match = router.match("GET", "/posts")
    assert match.matched
    assert match.handler() == "posts"

    # Test non-match
    match = router.match("GET", "/nonexistent")
    assert not match.matched
    assert match.handler is None

    print("[PASS] Static routes test passed")


def test_dynamic_parameters():
    """Test dynamic parameter extraction."""
    router = RadixRouter()

    def get_user(id): return f"user:{id}"
    def get_post(id): return f"post:{id}"
    def get_comment(post_id, comment_id): return f"post:{post_id}/comment:{comment_id}"

    router.add("GET", "/users/:id", get_user)
    router.add("GET", "/posts/:post_id", get_post)  # Use consistent param name
    router.add("GET", "/posts/:post_id/comments/:comment_id", get_comment)
    router.compile()

    # Test single parameter
    match = router.match("GET", "/users/123")
    assert match.matched
    assert match.params == {"id": "123"}

    match = router.match("GET", "/users/abc")
    assert match.matched
    assert match.params == {"id": "abc"}

    # Test different parameter names
    match = router.match("GET", "/posts/456")
    assert match.matched
    assert match.params == {"post_id": "456"}

    # Test multiple parameters
    match = router.match_new("GET", "/posts/10/comments/20")
    assert match.matched
    assert match.params == {"post_id": "10", "comment_id": "20"}

    print("[PASS] Dynamic parameters test passed")


def test_wildcard_routes():
    """Test wildcard catch-all routes."""
    router = RadixRouter()

    def serve_file(path): return f"file:{path}"
    def serve_static(rest): return f"static:{rest}"

    router.add("GET", "/files/*filepath", serve_file)
    router.add("GET", "/static/*", serve_static)  # Anonymous wildcard
    router.compile()

    # Test wildcard captures rest of path
    match = router.match("GET", "/files/docs/readme.md")
    assert match.matched
    assert match.params == {"filepath": "docs/readme.md"}

    match = router.match("GET", "/files/a/b/c/d.txt")
    assert match.matched
    assert match.params == {"filepath": "a/b/c/d.txt"}

    # Test single segment wildcard
    match = router.match("GET", "/files/single.txt")
    assert match.matched
    assert match.params == {"filepath": "single.txt"}

    print("[PASS] Wildcard routes test passed")


def test_priority_static_over_dynamic():
    """Test that static routes take priority over dynamic."""
    router = RadixRouter()

    def handler_list(): return "list"
    def handler_dynamic(id): return f"dynamic:{id}"

    router.add("GET", "/users/list", handler_list)
    router.add("GET", "/users/:id", handler_dynamic)
    router.compile()

    # Static should match first
    match = router.match("GET", "/users/list")
    assert match.matched
    assert match.handler() == "list"
    assert match.params == {}  # No params for static route

    # Dynamic should match for other values
    match = router.match("GET", "/users/123")
    assert match.matched
    assert match.params == {"id": "123"}

    print("[PASS] Priority static > dynamic test passed")


def test_priority_dynamic_over_wildcard():
    """Test that dynamic routes take priority over wildcard."""
    router = RadixRouter()

    def handler_dynamic(id): return f"dynamic:{id}"
    def handler_wildcard(rest): return f"wildcard:{rest}"

    router.add("GET", "/api/:version/endpoint", handler_dynamic)
    router.add("GET", "/api/*rest", handler_wildcard)
    router.compile()

    # Dynamic should match first for two segments after /api
    match = router.match("GET", "/api/v1/endpoint")
    assert match.matched
    assert match.params == {"version": "v1"}

    # Wildcard should match for paths without endpoint
    match = router.match("GET", "/api/other/stuff/here")
    assert match.matched
    assert match.params == {"rest": "other/stuff/here"}

    print("[PASS] Priority dynamic > wildcard test passed")


def test_method_separation():
    """Test that different methods have separate route trees."""
    router = RadixRouter()

    def get_handler(): return "GET"
    def post_handler(): return "POST"
    def put_handler(): return "PUT"
    def delete_handler(): return "DELETE"

    router.add("GET", "/resource", get_handler)
    router.add("POST", "/resource", post_handler)
    router.add("PUT", "/resource/:id", put_handler)
    router.add("DELETE", "/resource/:id", delete_handler)
    router.compile()

    # Each method should work independently
    match = router.match("GET", "/resource")
    assert match.matched
    assert match.handler() == "GET"

    match = router.match("POST", "/resource")
    assert match.matched
    assert match.handler() == "POST"

    match = router.match("PUT", "/resource/123")
    assert match.matched
    assert match.handler() == "PUT"
    assert match.params == {"id": "123"}

    match = router.match("DELETE", "/resource/456")
    assert match.matched
    assert match.handler() == "DELETE"
    assert match.params == {"id": "456"}

    # Wrong method should not match
    match = router.match("PATCH", "/resource")
    assert not match.matched

    print("[PASS] Method separation test passed")


def test_nested_routes():
    """Test deeply nested route structures."""
    router = RadixRouter()

    def handler_a(): return "a"
    def handler_ab(): return "ab"
    def handler_abc(): return "abc"
    def handler_abd(): return "abd"

    router.add("GET", "/a", handler_a)
    router.add("GET", "/a/b", handler_ab)
    router.add("GET", "/a/b/c", handler_abc)
    router.add("GET", "/a/b/d", handler_abd)
    router.compile()

    match = router.match("GET", "/a")
    assert match.matched and match.handler() == "a"

    match = router.match("GET", "/a/b")
    assert match.matched and match.handler() == "ab"

    match = router.match("GET", "/a/b/c")
    assert match.matched and match.handler() == "abc"

    match = router.match("GET", "/a/b/d")
    assert match.matched and match.handler() == "abd"

    print("[PASS] Nested routes test passed")


def test_duplicate_route_error():
    """Test that duplicate routes raise an error."""
    router = RadixRouter()

    def handler1(): pass
    def handler2(): pass

    router.add("GET", "/users", handler1)

    try:
        router.add("GET", "/users", handler2)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "Duplicate route" in str(e)

    print("[PASS] Duplicate route error test passed")


def test_conflicting_params_error():
    """Test that conflicting parameter names raise an error."""
    router = RadixRouter()

    def handler1(id): pass
    def handler2(user_id): pass

    router.add("GET", "/users/:id/profile", handler1)

    try:
        router.add("GET", "/users/:user_id/settings", handler2)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "Conflicting parameter names" in str(e)

    print("[PASS] Conflicting params error test passed")


def test_match_new_thread_safety():
    """Test that match_new returns independent RouteMatch objects."""
    router = RadixRouter()

    def handler(id): return id

    router.add("GET", "/users/:id", handler)
    router.compile()

    # Get two matches - they should be independent
    match1 = router.match_new("GET", "/users/1")
    match2 = router.match_new("GET", "/users/2")

    assert match1.params == {"id": "1"}
    assert match2.params == {"id": "2"}

    # Modifying one shouldn't affect the other
    match1.params["id"] = "modified"
    assert match2.params == {"id": "2"}

    print("[PASS] Thread safety test passed")


def test_edge_cases():
    """Test edge cases and boundary conditions."""
    router = RadixRouter()

    def handler(): return "ok"

    # Root path
    router.add("GET", "/", handler)

    # Path with trailing slash (treated as separate segment)
    router.add("GET", "/trailing", handler)

    router.compile()

    match = router.match("GET", "/")
    assert match.matched

    match = router.match("GET", "/trailing")
    assert match.matched

    # Empty path should match root
    match = router.match("GET", "")
    assert match.matched  # Empty path normalizes to /

    print("[PASS] Edge cases test passed")


def test_complex_api_example():
    """Test a realistic REST API structure."""
    router = RadixRouter()

    # Define handlers
    handlers = {
        "list_users": lambda: "list_users",
        "get_user": lambda id: f"get_user:{id}",
        "create_user": lambda: "create_user",
        "update_user": lambda id: f"update_user:{id}",
        "delete_user": lambda id: f"delete_user:{id}",
        "user_posts": lambda id: f"user_posts:{id}",
        "user_post": lambda uid, pid: f"user:{uid}/post:{pid}",
        "serve_static": lambda path: f"static:{path}",
    }

    # Register routes - use consistent param name :user_id throughout
    router.add("GET", "/api/v1/users", handlers["list_users"])
    router.add("GET", "/api/v1/users/:user_id", handlers["get_user"])
    router.add("POST", "/api/v1/users", handlers["create_user"])
    router.add("PUT", "/api/v1/users/:user_id", handlers["update_user"])
    router.add("DELETE", "/api/v1/users/:user_id", handlers["delete_user"])
    router.add("GET", "/api/v1/users/:user_id/posts", handlers["user_posts"])
    router.add("GET", "/api/v1/users/:user_id/posts/:post_id", handlers["user_post"])
    router.add("GET", "/static/*filepath", handlers["serve_static"])
    router.compile()

    # Test all routes
    match = router.match("GET", "/api/v1/users")
    assert match.matched and match.handler() == "list_users"

    match = router.match("GET", "/api/v1/users/42")
    assert match.matched and match.params == {"user_id": "42"}

    match = router.match("POST", "/api/v1/users")
    assert match.matched and match.handler() == "create_user"

    match = router.match("PUT", "/api/v1/users/42")
    assert match.matched and match.params == {"user_id": "42"}

    match = router.match("DELETE", "/api/v1/users/42")
    assert match.matched and match.params == {"user_id": "42"}

    match = router.match("GET", "/api/v1/users/42/posts")
    assert match.matched and match.params == {"user_id": "42"}

    match = router.match_new("GET", "/api/v1/users/42/posts/99")
    assert match.matched and match.params == {"user_id": "42", "post_id": "99"}

    match = router.match("GET", "/static/css/styles.css")
    assert match.matched and match.params == {"filepath": "css/styles.css"}

    print("[PASS] Complex API example test passed")


def test_get_routes():
    """Test route introspection."""
    router = RadixRouter()

    router.add("GET", "/a", lambda: None)
    router.add("POST", "/b", lambda: None)
    router.add("PUT", "/c/:id", lambda: None)

    routes = router.get_routes()
    assert len(routes) == 3
    assert ("GET", "/a") in routes
    assert ("POST", "/b") in routes
    assert ("PUT", "/c/:id") in routes

    print("[PASS] Get routes test passed")


def run_all_tests():
    """Run all tests."""
    print("\n" + "=" * 60)
    print("RadixRouter Test Suite")
    print("=" * 60 + "\n")

    test_static_routes()
    test_dynamic_parameters()
    test_wildcard_routes()
    test_priority_static_over_dynamic()
    test_priority_dynamic_over_wildcard()
    test_method_separation()
    test_nested_routes()
    test_duplicate_route_error()
    test_conflicting_params_error()
    test_match_new_thread_safety()
    test_edge_cases()
    test_complex_api_example()
    test_get_routes()

    print("\n" + "=" * 60)
    print("All tests passed!")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    run_all_tests()
