/**
 * Route guard that redirects unauthenticated users to /login.
 */

import { Navigate, Outlet } from "react-router-dom";
import { useAuth } from "../contexts/AuthContext";

/**
 * Wraps child routes and redirects to /login if no user is authenticated.
 * Shows a loading spinner while the auth state is being restored.
 */
export default function ProtectedRoute(): JSX.Element {
  const { isAuthenticated, loading } = useAuth();

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary-500 border-t-transparent" />
      </div>
    );
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  return <Outlet />;
}
