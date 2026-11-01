import { Navigate, Route, Routes } from "react-router-dom";
import Layout, { homePath } from "./components/Layout";
import { Spinner } from "./components/ui";
import { useAuth } from "./lib/auth";
import Notifications from "./pages/Notifications";
import AdminHome, { Structure, Teachers } from "./pages/admin/Admin";
import { Import, Rollover, Students } from "./pages/admin/AdminPeople";
import Login from "./pages/auth/Login";
import SetPassword from "./pages/auth/SetPassword";
import Signup, { ForgotPassword } from "./pages/auth/Signup";
import Welcome from "./pages/auth/Welcome";
import ParentHome, { ParentAssign, ParentAssignment, ParentChild } from "./pages/parent/Parent";
import Check from "./pages/student/Check";
import StudentHome from "./pages/student/Home";
import MyLearning from "./pages/student/Learning";
import StudentLesson from "./pages/student/Lesson";
import Progress from "./pages/student/Progress";
import Review from "./pages/student/Review";
import SelfLearning, { SelfDetail, SelfNew } from "./pages/student/SelfLearning";
import AssignmentDetail, { ReviewQueue } from "./pages/teacher/AssignmentDetail";
import LessonReview from "./pages/teacher/LessonReview";
import TeacherHome, { NewLesson, TeacherAssignments } from "./pages/teacher/Teacher";

function Guard({ home, children }: { home: string[]; children: JSX.Element }) {
  const { me } = useAuth();
  if (!me?.homes.some((h) => home.includes(h))) return <Navigate to={homePath(me?.homes || [])} replace />;
  return children;
}

export default function App() {
  const { status, me } = useAuth();
  if (status === "loading") return <div className="auth-wrap"><Spinner /></div>;
  if (status === "setPassword") return <SetPassword />;
  if (status === "signedOut")
    return (
      <Routes>
        <Route path="/signup" element={<Signup />} />
        <Route path="/forgot-password" element={<ForgotPassword />} />
        <Route path="*" element={<Login />} />
      </Routes>
    );
  if (status === "noAccount" || !me?.homes.length) return <Welcome />;
  const S = ["STUDENT", "INDEPENDENT"];
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Navigate to={homePath(me.homes)} replace />} />
        <Route path="/notifications" element={<Notifications />} />
        <Route path="/student" element={<Guard home={S}><StudentHome /></Guard>} />
        <Route path="/student/learning" element={<Guard home={S}><MyLearning /></Guard>} />
        <Route path="/student/progress" element={<Guard home={S}><Progress /></Guard>} />
        <Route path="/student/lesson/:sa" element={<Guard home={S}><StudentLesson /></Guard>} />
        <Route path="/student/check/:sa" element={<Guard home={S}><Check /></Guard>} />
        <Route path="/student/review/:itemId" element={<Guard home={S}><Review /></Guard>} />
        <Route path="/student/self" element={<Guard home={S}><SelfLearning /></Guard>} />
        <Route path="/student/self/new" element={<Guard home={S}><SelfNew /></Guard>} />
        <Route path="/student/self/:id" element={<Guard home={S}><SelfDetail /></Guard>} />
        <Route path="/teacher" element={<Guard home={["TEACHER"]}><TeacherHome /></Guard>} />
        <Route path="/teacher/lessons/new" element={<Guard home={["TEACHER"]}><NewLesson /></Guard>} />
        <Route path="/teacher/lessons/:id" element={<Guard home={["TEACHER"]}><LessonReview /></Guard>} />
        <Route path="/teacher/assignments" element={<Guard home={["TEACHER"]}><TeacherAssignments /></Guard>} />
        <Route path="/teacher/assignments/:id" element={<Guard home={["TEACHER"]}><AssignmentDetail /></Guard>} />
        <Route path="/teacher/review" element={<Guard home={["TEACHER"]}><ReviewQueue /></Guard>} />
        <Route path="/parent" element={<Guard home={["PARENT"]}><ParentHome /></Guard>} />
        <Route path="/parent/child/:studentId" element={<Guard home={["PARENT"]}><ParentChild /></Guard>} />
        <Route path="/parent/child/:studentId/assign" element={<Guard home={["PARENT"]}><ParentAssign /></Guard>} />
        <Route path="/parent/child/:studentId/assignment/:id" element={<Guard home={["PARENT"]}><ParentAssignment /></Guard>} />
        <Route path="/admin" element={<Guard home={["ADMIN"]}><AdminHome /></Guard>} />
        <Route path="/admin/structure" element={<Guard home={["ADMIN"]}><Structure /></Guard>} />
        <Route path="/admin/students" element={<Guard home={["ADMIN"]}><Students /></Guard>} />
        <Route path="/admin/teachers" element={<Guard home={["ADMIN"]}><Teachers /></Guard>} />
        <Route path="/admin/import" element={<Guard home={["ADMIN"]}><Import /></Guard>} />
        <Route path="/admin/rollover" element={<Guard home={["ADMIN"]}><Rollover /></Guard>} />
        <Route path="*" element={<Navigate to={homePath(me.homes)} replace />} />
      </Route>
    </Routes>
  );
}
