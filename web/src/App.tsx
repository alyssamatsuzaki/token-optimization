import { Navigate, Route, Routes } from "react-router-dom";
import Shell from "./components/Shell";
import Optimize from "./screens/Optimize";
import Inspect from "./screens/Inspect";
import Compare from "./screens/Compare";
import Spend from "./screens/Spend";
import Settings from "./screens/Settings";

export default function App() {
  return (
    <Shell>
      <Routes>
        <Route path="/" element={<Navigate to="/optimize" replace />} />
        <Route path="/optimize" element={<Optimize />} />
        <Route path="/inspect" element={<Inspect />} />
        <Route path="/compare" element={<Compare />} />
        <Route path="/spend" element={<Spend />} />
        <Route path="/settings" element={<Settings />} />
      </Routes>
    </Shell>
  );
}
