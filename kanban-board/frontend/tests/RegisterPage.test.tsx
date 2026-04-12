/**
 * Tests for the RegisterPage component.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { AuthProvider } from "../src/contexts/AuthContext";
import RegisterPage from "../src/pages/RegisterPage";
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
  email: "newuser@example.com",
  display_name: "New User",
  avatar_url: null,
  created_at: "2024-01-01T00:00:00Z",
};

/** Render RegisterPage inside required providers. */
function renderRegisterPage(): ReturnType<typeof render> {
  return render(
    <MemoryRouter initialEntries={["/register"]}>
      <AuthProvider>
        <RegisterPage />
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe("RegisterPage", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
  });

  it("should render the registration form with all fields", () => {
    renderRegisterPage();

    expect(
      screen.getByRole("heading", { name: /create an account/i }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/display name/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^email$/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^password$/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/confirm password/i)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /create account/i }),
    ).toBeInTheDocument();
  });

  it("should have a link to the login page", () => {
    renderRegisterPage();

    const loginLink = screen.getByRole("link", { name: /sign in/i });
    expect(loginLink).toBeInTheDocument();
    expect(loginLink).toHaveAttribute("href", "/login");
  });

  it("should show error when display name is empty", async () => {
    const user = userEvent.setup();
    renderRegisterPage();

    await user.type(screen.getByLabelText(/^email$/i), "newuser@example.com");
    await user.type(screen.getByLabelText(/^password$/i), "password123");
    await user.type(screen.getByLabelText(/confirm password/i), "password123");
    // Leave display name empty — clear the field in case browser auto-fills
    await user.clear(screen.getByLabelText(/display name/i));
    await user.click(screen.getByRole("button", { name: /create account/i }));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Display name is required.",
    );
    expect(mockPost).not.toHaveBeenCalled();
  });

  it("should show error when passwords do not match", async () => {
    const user = userEvent.setup();
    renderRegisterPage();

    await user.type(screen.getByLabelText(/display name/i), "New User");
    await user.type(screen.getByLabelText(/^email$/i), "newuser@example.com");
    await user.type(screen.getByLabelText(/^password$/i), "password123");
    await user.type(screen.getByLabelText(/confirm password/i), "different");
    await user.click(screen.getByRole("button", { name: /create account/i }));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Passwords do not match.",
    );
    // Should NOT call the API
    expect(mockPost).not.toHaveBeenCalled();
  });

  it("should show error when password is too short", async () => {
    const user = userEvent.setup();
    renderRegisterPage();

    await user.type(screen.getByLabelText(/display name/i), "New User");
    await user.type(screen.getByLabelText(/^email$/i), "newuser@example.com");
    await user.type(screen.getByLabelText(/^password$/i), "short");
    await user.type(screen.getByLabelText(/confirm password/i), "short");
    await user.click(screen.getByRole("button", { name: /create account/i }));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Password must be at least 8 characters.",
    );
    expect(mockPost).not.toHaveBeenCalled();
  });

  it("should call register and navigate on successful submission", async () => {
    const user = userEvent.setup();
    mockPost.mockResolvedValueOnce({ data: mockTokenPair });
    mockGet.mockResolvedValueOnce({ data: mockUser });

    renderRegisterPage();

    await user.type(screen.getByLabelText(/display name/i), "New User");
    await user.type(screen.getByLabelText(/^email$/i), "newuser@example.com");
    await user.type(screen.getByLabelText(/^password$/i), "password123");
    await user.type(screen.getByLabelText(/confirm password/i), "password123");
    await user.click(screen.getByRole("button", { name: /create account/i }));

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith("/auth/register", {
        email: "newuser@example.com",
        password: "password123",
        display_name: "New User",
      });
    });

    await waitFor(() => {
      expect(mockNavigate).toHaveBeenCalledWith("/workspaces", {
        replace: true,
      });
    });
  });

  it("should display server error on API failure", async () => {
    const user = userEvent.setup();
    mockPost.mockRejectedValueOnce({
      response: { data: { detail: "Email already registered" } },
    });

    renderRegisterPage();

    await user.type(screen.getByLabelText(/display name/i), "Existing User");
    await user.type(screen.getByLabelText(/^email$/i), "existing@example.com");
    await user.type(screen.getByLabelText(/^password$/i), "password123");
    await user.type(screen.getByLabelText(/confirm password/i), "password123");
    await user.click(screen.getByRole("button", { name: /create account/i }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Email already registered",
      );
    });
  });

  it("should display generic error when no detail is returned", async () => {
    const user = userEvent.setup();
    mockPost.mockRejectedValueOnce(new Error("Network Error"));

    renderRegisterPage();

    await user.type(screen.getByLabelText(/display name/i), "New User");
    await user.type(screen.getByLabelText(/^email$/i), "newuser@example.com");
    await user.type(screen.getByLabelText(/^password$/i), "password123");
    await user.type(screen.getByLabelText(/confirm password/i), "password123");
    await user.click(screen.getByRole("button", { name: /create account/i }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Registration failed. Please try again.",
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

    renderRegisterPage();

    await user.type(screen.getByLabelText(/display name/i), "New User");
    await user.type(screen.getByLabelText(/^email$/i), "newuser@example.com");
    await user.type(screen.getByLabelText(/^password$/i), "password123");
    await user.type(screen.getByLabelText(/confirm password/i), "password123");
    await user.click(screen.getByRole("button", { name: /create account/i }));

    // Button should show loading text and be disabled
    const button = screen.getByRole("button", { name: /creating account/i });
    expect(button).toBeDisabled();

    // Resolve to clean up
    resolvePost!({ data: mockTokenPair });
    mockGet.mockResolvedValueOnce({ data: mockUser });

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /create account/i }),
      ).not.toBeDisabled();
    });
  });

  it("should clear previous error when form is re-submitted", async () => {
    const user = userEvent.setup();
    renderRegisterPage();

    // First, trigger a validation error
    await user.type(screen.getByLabelText(/display name/i), "New User");
    await user.type(screen.getByLabelText(/^email$/i), "newuser@example.com");
    await user.type(screen.getByLabelText(/^password$/i), "password123");
    await user.type(screen.getByLabelText(/confirm password/i), "mismatch");
    await user.click(screen.getByRole("button", { name: /create account/i }));
    expect(screen.getByRole("alert")).toBeInTheDocument();

    // Now fix the confirm password and re-submit (mock a success)
    mockPost.mockResolvedValueOnce({ data: mockTokenPair });
    mockGet.mockResolvedValueOnce({ data: mockUser });

    await user.clear(screen.getByLabelText(/confirm password/i));
    await user.type(screen.getByLabelText(/confirm password/i), "password123");
    await user.click(screen.getByRole("button", { name: /create account/i }));

    await waitFor(() => {
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });
  });
});
