// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// Draws the wires and packets for a diagram page. Nothing carries a coordinate: the
// CSS grids place the boxes, this measures them. The page defines EDGES before
// loading this file, one [from, to, shape, laneOffset, label, class, stage] per wire.
//
// Shapes: h  out the right face, level with the source's centre, into the left face
//         v  bottom face down to the top face      vup  top face up to the bottom face
//         hvh  right face, turn in the gap at laneOffset px, left face of another row
//         hru  right face, then up into the target's bottom face
//         ur   top face up, then right into the left face of a box in a higher row
//         vl   bottom face down, then left into the right face of a box in a lower row
//
// ?t=0..1 is the loop position: render.sh samples one frame per t, because
// animateMotion under a virtual clock is not reproducible.
(() => {
  const canvas = document.getElementById("canvas");
  const svg = document.getElementById("wires");
  const NS = "http://www.w3.org/2000/svg";
  const G = 7; // stop short of the box so the arrowhead reads

  const R = id => {
    const el = document.getElementById(id);
    if (!el) throw new Error(`no box with id ${id}`);
    const c = canvas.getBoundingClientRect(), n = el.getBoundingClientRect();
    return { x: n.left - c.left, y: n.top - c.top, r: n.right - c.left, b: n.bottom - c.top,
             cx: n.left - c.left + n.width / 2, cy: n.top - c.top + n.height / 2 };
  };

  function route(from, to, shape, k) {
    const a = R(from), b = R(to);
    switch (shape) {
      case "h":   return [{ x: a.r, y: a.cy }, { x: b.x - G, y: a.cy }];
      case "v":   return [{ x: a.cx + k, y: a.b }, { x: a.cx + k, y: b.y - G }];
      case "vup": return [{ x: a.cx + k, y: a.y }, { x: a.cx + k, y: b.b + G }];
      case "hvh": return [{ x: a.r, y: a.cy }, { x: a.r + k, y: a.cy },
                          { x: a.r + k, y: b.cy }, { x: b.x - G, y: b.cy }];
      case "ur":  return [{ x: a.cx + k, y: a.y }, { x: a.cx + k, y: b.cy }, { x: b.x - G, y: b.cy }];
      case "vl":  return [{ x: a.cx + k, y: a.b }, { x: a.cx + k, y: b.cy }, { x: b.r + G, y: b.cy }];
      case "hru": return [{ x: a.r, y: a.cy }, { x: b.cx + k, y: a.cy }, { x: b.cx + k, y: b.b + G }];
    }
    throw new Error(`unknown shape ${shape}`);
  }

  // rounded polyline through a point list
  function d(pts, rad = 9) {
    let s = `M${pts[0].x} ${pts[0].y}`;
    for (let i = 1; i < pts.length - 1; i++) {
      const p = pts[i], a = pts[i - 1], n = pts[i + 1];
      const v1 = Math.sign(p.x - a.x), w1 = Math.sign(p.y - a.y);
      const v2 = Math.sign(n.x - p.x), w2 = Math.sign(n.y - p.y);
      const r = Math.min(rad, Math.hypot(p.x - a.x, p.y - a.y) / 2, Math.hypot(n.x - p.x, n.y - p.y) / 2);
      s += `L${p.x - v1 * r} ${p.y - w1 * r}Q${p.x} ${p.y} ${p.x + v2 * r} ${p.y + w2 * r}`;
    }
    const e = pts.at(-1);
    return s + `L${e.x} ${e.y}`;
  }

  svg.innerHTML = `<defs>
    <marker id="a1" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6.5" markerHeight="6.5" orient="auto">
      <path d="M0 0.7 L7.4 4 L0 7.3 Z" fill="hsl(220 10% 55%)"/></marker>
    <marker id="a2" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6.5" markerHeight="6.5" orient="auto">
      <path d="M0 0.7 L7.4 4 L0 7.3 Z" fill="hsl(220 12% 72%)"/></marker>
  </defs>`;

  const paths = [];
  for (const [f, t, shape, k = 0, label = "", cls = "", stage = 1] of EDGES) {
    const pts = route(f, t, shape, k);
    const p = document.createElementNS(NS, "path");
    p.setAttribute("d", d(pts));
    p.setAttribute("class", "wire " + cls);
    p.setAttribute("marker-end", /dash/.test(cls) ? "url(#a2)" : "url(#a1)");
    svg.appendChild(p);
    paths.push({ path: p, cls, stage });
    if (!label) continue;

    // the label rides the longest segment: above it when horizontal, beside it when
    // vertical, or the wire runs through the glyphs
    let best = 0, bx = 0, by = 0, horiz = true;
    for (let i = 1; i < pts.length; i++) {
      const L = Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y);
      if (L > best) {
        best = L; bx = (pts[i].x + pts[i - 1].x) / 2; by = (pts[i].y + pts[i - 1].y) / 2;
        horiz = Math.abs(pts[i].y - pts[i - 1].y) < 2;
      }
    }
    // a vertical wire offset left of its box's centre takes its label on the left,
    // so two wires sharing a column never label across each other
    const left = !horiz && k < 0;
    const tx = document.createElementNS(NS, "text");
    tx.setAttribute("x", horiz ? bx : left ? bx - 7 : bx + 7);
    tx.setAttribute("y", horiz ? by - 6 : by + 3);
    tx.setAttribute("text-anchor", horiz ? "middle" : left ? "end" : "start");
    tx.setAttribute("class", "wtag " + cls);
    tx.textContent = label;
    svg.appendChild(tx);
  }

  // packets: each stage lags the one before, so the page reads as one wave; the
  // trail is in px so a comet looks the same on a short wire and a long one
  const TIME = Number(new URLSearchParams(location.search).get("t") || 0);
  const TRAIL = [[0, 4, 1], [-7, 2.6, 0.5], [-13, 1.7, 0.22]];
  for (const { path, cls, stage } of paths) {
    const len = path.getTotalLength();
    const head = (((TIME - stage * 0.13) % 1) + 1) % 1;
    for (const [px, r, op] of TRAIL) {
      const at = head + px / len;
      if (at < 0) continue;
      const q = path.getPointAtLength(len * at);
      const c = document.createElementNS(NS, "circle");
      c.setAttribute("cx", q.x); c.setAttribute("cy", q.y); c.setAttribute("r", r);
      c.setAttribute("opacity", op);
      c.setAttribute("class", "pkt " + cls);
      svg.appendChild(c);
    }
  }
})();
