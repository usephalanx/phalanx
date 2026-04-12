/**
 * Tests for the AuthContext provider.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, act, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AuthProvider, useAuth } from "../src/contexts/AuthContext";
import type { TokenPair, User } from "../src/types/auth";

// Mock the API client
vi.mock("../src/api/client", () => ({
  default: {
    post: vi.fn(),
    get: vi.fn(),
  },
}));

import apiClient from "../src/api/client";

const mockPost = vi.mocked(apiClient.post);
const mockGet = vi.mocked(apiClient.get);

const mockTokenPair: TokenPair = {
  access_token: "test-access-token",
  refresh_token: "test-refresh-token",
  token_type: "bearer",
};

const mockUser: User = {
  id: "550e8400-e29b-41d4-a716-446655440000",
  email: "test@example.com",
  display_name: "Test User",
  avatar_url: null,
  created_at: "2024-01-01T00:00:00Z",
};

/**
 * Helper component that exposes auth context values for testing.
 */
function TestConsumer(): JSX.Element {
  const { user, isAuthenticated, loading, login, logout } = useAuth();

  return (
    <div>
      <span data-testid="loading">{String(loading)}</span>
      <span data-testid="user">{user ? user.email : "null"}</span>
      <span data-testid="is-authenticated">{String(isAuthenticated)}</span>
      <button
        data-testid="login-btn"
        onClick={() =>
          void login({ email: "test@example.com", password: "password" })
        }
      >
        Login
      </button>
      <button data-testid="logout-btn" onClick={logout}>
        Logout
      </button>
    </div>
  );
}

describe("AuthContext", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
  });

  it("should start with no user when localStorage is empty", async () => {
    // Mock the /auth/me call that happens on mount verification
    mockGet.mockRejectedValueOnce(new Error("No token"));

    render(
      <AuthProvider>
        <TestConsumer />
      </AuthProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("loading").textContent).toBe("false");
    });

    expect(screen.getByTestId("user").textContent).toBe("null");
    expect(screen.getByTestId("is-authenticated").textContent).toBe("false");
  });

  it("should restore user from localStorage on mount and verify token", async () => {
    localStorage.setItem("access_token", "stored-token");
    localStorage.setItem("user", JSON.stringify(mockUser));

    // The AuthContext verifies by calling /auth/me on mount
    mockGet.mockResolvedValueOnce({ data: mockUser });

    render(
      <AuthProvider>
        <TestConsumer />
      </AuthProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("loading").textContent).toBe("false");
    });

    expect(screen.getByTestId("user").textContent).toBe("test@example.com");
    expect(screen.getByTestId("is-authenticated").textContent).toBe("true");
  });

  it("should set user and tokens on login", async () => {
    // Mount with no stored auth — /auth/me won't be called on mount
    mockPost.mockResolvedValueOnce({ data: mockTokenPair });
    mockGet.mockResolvedValueOnce({ data: mockUser });

    render(
      <AuthProvider>
        <TestConsumer />
      </AuthProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("loading").textContent).toBe("false");
    });

    await act(async () => {
      await userEvent.click(screen.getByTestId("login-btn"));
    });

    expect(mockPost).toHaveBeenCalledWith("/auth/login", {
      email: "test@example.com",
      password: "password",
    });
    expect(mockGet).toHaveBeenCalledWith("/auth/me");
    expect(screen.getByTestId("user").textContent).toBe("test@example.com");
    expect(screen.getByTestId("is-authenticated").textContent).toBe("true");
    expect(localStorage.getItem("access_token")).toBe("test-access-token");
    expect(localStorage.getItem("refresh_token")).toBe("test-refresh-token");
  });

  it("should clear user and tokens on logout", async () => {
    localStorage.setItem("access_token", "stored-token");
    localStorage.setItem("refresh_token", "stored-refresh");
    localStorage.setItem("user", JSON.stringify(mockUser));

    // Mock the /auth/me verification call on mount
    mockGet.mockResolvedValueOnce({ data: mockUser });

    render(
      <AuthProvider>
        <TestConsumer />
      </AuthProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("user").textContent).toBe("test@example.com");
    });

    expect(screen.getByTestId("is-authenticated").textContent).toBe("true");

    await act(async () => {
      await userEvent.click(screen.getByTestId("logout-btn"));
    });

    expect(screen.getByTestId("user").textContent).toBe("null");
    expect(screen.getByTestId("is-authenticated").textContent).toBe("false");
    expect(localStorage.getItem("access_token")).toBeNull();
    expect(localStorage.getItem("refresh_token")).toBeNull();
    expect(localStorage.getItem("user")).toBeNull();
  });

  it("should attempt token refresh when /auth/me fails on mount", async () => {
    localStorage.setItem("access_token", "expired-token");
    localStorage.setItem("refresh_token", "valid-refresh-token");
    localStorage.setItem("user", JSON.stringify(mockUser));

    // First /auth/me call fails (expired token)
    mockGet.mockRejectedValueOnce({ response: { status: 401 } });
    // Refresh call succeeds
    mockPost.mockResolvedValueOnce({
      data: { access_token: "new-access-token", token_type: "bearer" },
    });
    // Second /auth/me call succeeds with new token
    mockGet.mockResolvedValueOnce({ data: mockUser });

    render(
      <AuthProvider>
        <TestConsumer />
      </AuthProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("loading").textContent).toBe("false");
    });

    expect(screen.getByTestId("user").textContent).toBe("test@example.com");
    expect(screen.getByTestId("is-authenticated").textContent).toBe("true");
    expect(localStorage.getItem("access_token")).toBe("new-access-token");
  });

  it("should clear auth when both token verification and refresh fail", async () => {
    localStorage.setItem("access_token", "expired-token");
    localStorage.setItem("refresh_token", "expired-refresh-token");
    localStorage.setItem("user", JSON.stringify(mockUser));

    // /auth/me fails
    mockGet.mockRejectedValueOnce({ response: { status: 401 } });
    // Refresh also fails
    mockPost.mockRejectedValueOnce({ response: { status: 401 } });

    render(
      <AuthProvider>
        <TestConsumer />
      </AuthProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("loading").textContent).toBe("false");
    });

    expect(screen.getByTestId("user").textContent).toBe("null");
    expect(screen.getByTestId("is-authenticated").textContent).toBe("false");
    expect(localStorage.getItem("access_token")).toBeNull();
    expect(localStorage.getItem("refresh_token")).toBeNull();
  });

  it("should throw when useAuth is used outside AuthProvider", () => {
    // Suppress console.error for this test
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});

    expect(() => render(<TestConsumer />)).toThrow(
      "useAuth must be used within an AuthProvider",
    );

    spy.mockRestore();
  });
});
