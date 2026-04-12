/**
 * Tests for the LoginPage component.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { AuthProvider } from "../src/contexts/AuthContext";
import LoginPage from "../src/pages/LoginPage";
import type { TokenPair, User } from "../src/types/auth";

// Mock react-router-dom navigate
const mockNavigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual("react-router-dom");
  return {
    ...actual,
    useNavigate: () => mockNavigate,
  };
});

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

/** Render LoginPage inside required providers. */
function renderLoginPage(): ReturnType<typeof render> {
  return render(
    <MemoryRouter initialEntries={["/login"]}>
      <AuthProvider>
        <LoginPage />
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe("LoginPage", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
  });

  it("should render the login form with all fields", () => {
    renderLoginPage();

    expect(
      screen.getByRole("heading", { name: /sign in to kanban board/i }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/email/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /sign in/i }),
    ).toBeInTheDocument();
  });

  it("should have a link to the register page", () => {
    renderLoginPage();

    const registerLink = screen.getByRole("link", { name: /register/i });
    expect(registerLink).toBeInTheDocument();
    expect(registerLink).toHaveAttribute("href", "/register");
  });

  it("should call login and navigate on successful submission", async () => {
    const user = userEvent.setup();
    mockPost.mockResolvedValueOnce({ data: mockTokenPair });
    mockGet.mockResolvedValueOnce({ data: mockUser });

    renderLoginPage();

    await user.type(screen.getByLabelText(/email/i), "test@example.com");
    await user.type(screen.getByLabelText(/password/i), "password123");
    await user.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith("/auth/login", {
        email: "test@example.com",
        password: "password123",
      });
    });

    await waitFor(() => {
      expect(mockNavigate).toHaveBeenCalledWith("/workspaces", {
        replace: true,
      });
    });
  });

  it("should display an error message on login failure", async () => {
    const user = userEvent.setup();
    mockPost.mockRejectedValueOnce({
      response: { data: { detail: "Invalid email or password" } },
    });

    renderLoginPage();

    await user.type(screen.getByLabelText(/email/i), "test@example.com");
    await user.type(screen.getByLabelText(/password/i), "wrongpassword");
    await user.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Invalid email or password",
      );
    });
  });

  it("should display a generic error when no detail is returned", async () => {
    const user = userEvent.setup();
    mockPost.mockRejectedValueOnce(new Error("Network Error"));

    renderLoginPage();

    await user.type(screen.getByLabelText(/email/i), "test@example.com");
    await user.type(screen.getByLabelText(/password/i), "password123");
    await user.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Login failed. Please try again.",
      );
    });
  });

  it("should show loading state on the submit button", async () => {
    const user = userEvent.setup();
    // Make the post hang so we can check the loading state
    let resolvePost: (value: unknown) => void;
    mockPost.mockReturnValueOnce(
      new Promise((resolve) => {
        resolvePost = resolve;
      }) as ReturnType<typeof apiClient.post>,
    );

    renderLoginPage();

    await user.type(screen.getByLabelText(/email/i), "test@example.com");
    await user.type(screen.getByLabelText(/password/i), "password123");
    await user.click(screen.getByRole("button", { name: /sign in/i }));

    // Button should show loading text and be disabled
    const button = screen.getByRole("button", { name: /signing in/i });
    expect(button).toBeDisabled();

    // Resolve to clean up
    resolvePost!({ data: mockTokenPair });
    mockGet.mockResolvedValueOnce({ data: mockUser });

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /sign in/i }),
      ).not.toBeDisabled();
    });
  });
});
