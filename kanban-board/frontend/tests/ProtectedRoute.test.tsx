/**
 * Tests for the ProtectedRoute component.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { AuthProvider } from "../src/contexts/AuthContext";
import ProtectedRoute from "../src/components/ProtectedRoute";

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

describe("ProtectedRoute", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
  });

  it("should redirect to /login when user is not authenticated", () => {
    render(
      <MemoryRouter initialEntries={["/workspaces"]}>
        <AuthProvider>
          <Routes>
            <Route path="/login" element={<div>Login Page</div>} />
            <Route element={<ProtectedRoute />}>
              <Route
                path="/workspaces"
                element={<div>Workspaces Page</div>}
              />
            </Route>
          </Routes>
        </AuthProvider>
      </MemoryRouter>,
    );

    expect(screen.getByText("Login Page")).toBeInTheDocument();
    expect(screen.queryByText("Workspaces Page")).not.toBeInTheDocument();
  });

  it("should render the child route when user is authenticated", () => {
    localStorage.setItem("access_token", "test-token");
    localStorage.setItem(
      "user",
      JSON.stringify({
        id: "550e8400-e29b-41d4-a716-446655440000",
        email: "test@example.com",
        created_at: "2024-01-01T00:00:00Z",
      }),
    );

    render(
      <MemoryRouter initialEntries={["/workspaces"]}>
        <AuthProvider>
          <Routes>
            <Route path="/login" element={<div>Login Page</div>} />
            <Route element={<ProtectedRoute />}>
              <Route
                path="/workspaces"
                element={<div>Workspaces Page</div>}
              />
            </Route>
          </Routes>
        </AuthProvider>
      </MemoryRouter>,
    );

    expect(screen.getByText("Workspaces Page")).toBeInTheDocument();
    expect(screen.queryByText("Login Page")).not.toBeInTheDocument();
  });
});
