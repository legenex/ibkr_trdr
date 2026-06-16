import { Card, Eyebrow, EmptyState } from "../components/Primitives";

// Placeholder for routes beyond the Command page in this first pass.
export function Stub({ name }: { name: string }) {
  return (
    <div className="grid">
      <div>
        <Eyebrow>{name}</Eyebrow>
        <h1 className="display" style={{ fontSize: 26, margin: "6px 0 0" }}>
          {name}
        </h1>
      </div>
      <Card>
        <EmptyState title="Not built yet">
          This panel is part of a later pass. The Command page is the hero of this build; the
          remaining instruments (approvals queue, positions, audit, skills, learning, holdout meter)
          share this design system and slot in next.
        </EmptyState>
      </Card>
    </div>
  );
}
