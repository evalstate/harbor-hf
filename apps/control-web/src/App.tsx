import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { ApiError, type SessionResponse, signOut } from "./api";
import { ControlStateProvider, type DisplayActor } from "./control-state";
import { Layout } from "./layout";
import {
  AuditPage,
  CampaignPage,
  CampaignsPage,
  EndpointsPage,
  JobsPage,
  LeaderboardPage,
  NotFoundPage,
  OverviewPage,
  ProfilesPage,
  ResultPage,
  ResultsPage,
  TaskPage,
} from "./pages";
import { keys, useLiveUpdates, useSession, useSystem } from "./queries";
import { Button, Card, ErrorNotice, Loading, QueryContent } from "./ui";

function isPublicBoard(path: string): boolean {
  return path === "/" || path === "/leaderboard";
}

function GuestShell({ children }: { children: ReactNode }) {
  return (
    <Layout
      actor={null}
      writeMode="unknown"
      live={null}
      serviceError={null}
      onSignOut={() => undefined}
      signingOut={false}
    >
      {children}
    </Layout>
  );
}

function SignInCard({ returnTo }: { returnTo: string }) {
  return (
    <Card className="mx-auto mt-16 max-w-md text-center">
      <div className="mx-auto grid h-12 w-12 place-items-center rounded-xl bg-cyan-400 text-xl font-black text-slate-950">
        H
      </div>
      <h1 className="mt-5 text-xl font-semibold">Harbor-HF Control</h1>
      <p className="mt-2 text-sm leading-6 text-slate-400">
        Admin views require a Hugging Face login. Your session may have expired, been
        revoked, or been lost during a service restart.
      </p>
      <a
        className="mt-6 inline-block"
        href={`/auth/login?return_to=${encodeURIComponent(returnTo)}`}
      >
        <Button>Sign in with Hugging Face</Button>
      </a>
    </Card>
  );
}

function AuthenticatedApp({
  actor,
  expiresAt,
  sessionError,
}: {
  actor: DisplayActor;
  expiresAt?: string | undefined;
  sessionError: unknown;
}) {
  const client = useQueryClient();
  const system = useSystem();
  const live = useLiveUpdates(
    Boolean(system.data),
    system.data?.projection.event_cursor,
  );
  const logout = useMutation({
    mutationFn: signOut,
    onSuccess: () => {
      client.setQueryData<SessionResponse>(keys.session, {
        authenticated: false,
        login_url: "/auth/login",
      });
      client.removeQueries({ predicate: (query) => query.queryKey[0] !== "session" });
    },
  });
  const writeMode = system.data?.write_mode ?? "unknown";
  return (
    <ControlStateProvider actor={actor} writeMode={writeMode}>
      <Layout
        actor={actor}
        writeMode={writeMode}
        live={live}
        sessionExpiresAt={expiresAt}
        serviceError={sessionError ?? (system.data ? system.error : null)}
        onSignOut={() => logout.mutate()}
        signingOut={logout.isPending}
      >
        <Routes>
          <Route path="/" element={<LeaderboardPage />} />
          <Route path="/leaderboard" element={<Navigate to="/" replace />} />
          {system.data ? (
            <>
              <Route path="/overview" element={<OverviewPage />} />
              <Route path="/runs" element={<CampaignsPage />} />
              <Route path="/runs/:campaignId" element={<CampaignPage />} />
              <Route path="/runs/:campaignId/tasks/:taskId" element={<TaskPage />} />
              <Route path="/campaigns" element={<CampaignsPage />} />
              <Route path="/campaigns/:campaignId" element={<CampaignPage />} />
              <Route
                path="/campaigns/:campaignId/tasks/:taskId"
                element={<TaskPage />}
              />
              <Route path="/jobs" element={<JobsPage />} />
              <Route path="/endpoints" element={<EndpointsPage />} />
              <Route path="/results" element={<ResultsPage />} />
              <Route path="/results/:publicationId" element={<ResultPage />} />
              <Route path="/profiles" element={<ProfilesPage />} />
              <Route path="/audit" element={<AuditPage />} />
              <Route path="*" element={<NotFoundPage />} />
            </>
          ) : (
            <Route
              path="*"
              element={<QueryContent query={system}>{null}</QueryContent>}
            />
          )}
        </Routes>
      </Layout>
    </ControlStateProvider>
  );
}

export default function App() {
  const session = useSession();
  const location = useLocation();
  if (session.isPending && !session.data)
    return (
      <div className="min-h-screen bg-slate-950 text-slate-100">
        <Loading />
      </div>
    );

  const actor =
    session.data?.authenticated === true && session.data.actor
      ? session.data.actor
      : null;

  if (actor) {
    return (
      <AuthenticatedApp
        actor={actor}
        expiresAt={session.data?.expires_at}
        sessionError={session.error}
      />
    );
  }

  if (isPublicBoard(location.pathname)) {
    if (location.pathname === "/leaderboard") return <Navigate to="/" replace />;
    return (
      <GuestShell>
        <LeaderboardPage />
      </GuestShell>
    );
  }

  const unauthorized =
    (session.error instanceof ApiError && session.error.status === 401) ||
    session.data?.authenticated === false;
  if (unauthorized) {
    // Private Space embeds add signed query parameters that must not enter OAuth state.
    return (
      <GuestShell>
        <SignInCard returnTo={location.pathname} />
      </GuestShell>
    );
  }

  const error =
    session.error ?? new Error("The control service could not verify your session.");
  return (
    <GuestShell>
      <div className="mx-auto mt-16 w-full max-w-xl">
        <ErrorNotice error={error} retry={() => void session.refetch()} />
        <p className="mt-4 text-center text-sm text-slate-500">
          A temporary failure does not end an existing 12-hour session.
        </p>
      </div>
    </GuestShell>
  );
}
