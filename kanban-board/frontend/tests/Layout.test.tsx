/**
 * Tests for the Layout component.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { AuthProvider } from "../src/contexts/AuthContext";
import Layout from "../src/components/Layout";

// Mock the API client
vi.mock("../src/api/client", () => ({
  default: {
    post: vi.fn(),
    get: vi.fn(),
    interceptors: {
      request: { use: vi.fn() },
      response: { use: vi.fn() },
    },
    defaults: { headers: { common: {} } },
  },
}));

import apiClient from "../src/api/client";

const mockGet = vi.mocked(apiClient.get);

const mockUser = {
  id: "550e8400-e29b-41d4-a716-446655440000",
  email: "test@example.com",
  display_name: "Test User",
  avatar_url: null,
  created_at: "2024-01-01T00:00:00Z",
};

describe("Layout", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
  });

  function renderWithAuth(initialRoute = "/workspaces"): void {
    localStorage.setItem("access_token", "test-token");
    localStorage.setItem("user", JSON.stringify(mockUser));

    // Mock the /auth/me verification call that happens on mount
    mockGet.mockResolvedValueOnce({ data: mockUser });

    render(
      <MemoryRouter initialEntries={[initialRoute]}>
        <AuthProvider>
          <Routes>
            <Route path="/login" element={<div>Login Page</div>} />
            <Route element={<Layout />}>
              <Route
                path="/workspaces"
                element={<div>Workspaces Content</div>}
              />
            </Route>
          </Routes>
        </AuthProvider>
      </MemoryRouter>,
    );
  }

  it("should render the logo linking to /workspaces", () => {
    renderWithAuth();

    const logoLink = screen.getByRole("link", { name: /kanban/i });
    expect(logoLink).toBeInTheDocument();
    expect(logoLink).toHaveAttribute("href", "/workspaces");
  });

  it("should render the workspace switcher placeholder", () => {
    renderWithAuth();

    const switcherButton = screen.getByRole("button", {
      name: /switch workspace/i,
    });
    expect(switcherButton).toBeInTheDocument();
  });

  it("should display the user email", () => {
    renderWithAuth();

    expect(screen.getByText("test@example.com")).toBeInTheDocument();
  });

  it("should render the sign out button", () => {
    renderWithAuth();

    expect(
      screen.getByRole("button", { name: /sign out/i }),
    ).toBeInTheDocument();
  });

  it("should render child route content via Outlet", () => {
    renderWithAuth();

    expect(screen.getByText("Workspaces Content")).toBeInTheDocument();
  });

  it("should clear auth and navigate to /login on sign out", async () => {
    renderWithAuth();

    const signOutButton = screen.getByRole("button", { name: /sign out/i });
    await userEvent.click(signOutButton);

    expect(localStorage.getItem("access_token")).toBeNull();
    expect(localStorage.getItem("user")).toBeNull();
    expect(screen.getByText("Login Page")).toBeInTheDocument();
  });
});
