/**
 * Root application component with React Router setup.
 *
 * Routes:
 *   /login           — Login page (public)
 *   /register        — Registration page (public)
 *   /workspaces      — Workspace list (protected, with layout)
 *   /workspaces/:id  — Workspace detail (protected, with layout)
 *   /boards/:id      — Board detail with columns (protected, with layout)
 */

import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider } from "./contexts/AuthContext";
import Layout from "./components/Layout";
import ProtectedRoute from "./components/ProtectedRoute";
import LoginPage from "./pages/LoginPage";
import RegisterPage from "./pages/RegisterPage";
import WorkspacesPage from "./pages/WorkspacesPage";
import WorkspaceDetailPage from "./pages/WorkspaceDetailPage";
import BoardPage from "./pages/BoardPage";

/**
 * Top-level App component providing auth context, routing, and shared layout.
 */
export default function App(): JSX.Element {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          {/* Public routes */}
          <Route path="/login" element={<LoginPage />} />
          <Route path="/register" element={<RegisterPage />} />

          {/* Protected routes with shared layout (nav bar) */}
          <Route element={<ProtectedRoute />}>
            <Route element={<Layout />}>
              <Route path="/workspaces" element={<WorkspacesPage />} />
              <Route path="/workspaces/:id" element={<WorkspaceDetailPage />} />
              <Route path="/boards/:id" element={<BoardPage />} />
            </Route>
          </Route>

          {/* Catch-all redirect */}
          <Route path="*" element={<Navigate to="/workspaces" replace />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
