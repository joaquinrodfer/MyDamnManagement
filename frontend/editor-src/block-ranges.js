// De árbol de Markdown a BLOQUES: la lista de trozos de texto que la app trata
// como bloques (asa, `+`, Alt+↑/↓, arrastre, «Convertir a»...).
//
// Vive aparte de entry.js para que el cálculo sea el mismo que usan los tests
// de `node --test` sobre el árbol de verdad (`markdownLanguage.parser`), y no
// una copia con párrafos inventados: mover un bloque depende de dónde acaban
// los rangos, y una copia que se desviara escondería justo los fallos que se
// buscan.
//
// Las vallas de código son UN bloque (FencedCode va de la apertura al cierre),
// y una lista plana son N bloques (uno por ListItem hijo directo): una lista
// anidada queda dentro del ítem, así que mover el ítem se lleva sus hijos.

export function blocksFromTree(root) {
  const blocks = [];
  for (let node = root.firstChild; node; node = node.nextSibling) {
    switch (node.name) {
      case "BlankLine":
      case "CommentBlock":
        break;
      case "BulletList":
      case "OrderedList": {
        const itemType = node.name === "BulletList" ? "bullet_list_item" : "ordered_list_item";
        for (let item = node.firstChild; item; item = item.nextSibling) {
          if (item.name === "ListItem") blocks.push({ from: item.from, to: item.to, type: itemType });
        }
        break;
      }
      case "ATXHeading1":
        blocks.push({ from: node.from, to: node.to, type: "h1" });
        break;
      case "ATXHeading2":
        blocks.push({ from: node.from, to: node.to, type: "h2" });
        break;
      case "ATXHeading3":
        blocks.push({ from: node.from, to: node.to, type: "h3" });
        break;
      case "ATXHeading4":
        blocks.push({ from: node.from, to: node.to, type: "h4" });
        break;
      case "ATXHeading5":
      case "ATXHeading6":
        blocks.push({ from: node.from, to: node.to, type: "h5" });
        break;
      case "HorizontalRule":
        blocks.push({ from: node.from, to: node.to, type: "hr" });
        break;
      case "FencedCode":
      case "CodeBlock":
        blocks.push({ from: node.from, to: node.to, type: "code" });
        break;
      case "Paragraph":
        blocks.push({ from: node.from, to: node.to, type: "paragraph" });
        break;
      case "Blockquote":
        // Un solo bloque para toda la cita, con independencia de cuántos
        // niveles anidados (">", ">>"...) tenga dentro -- solo se recorren
        // los hijos DIRECTOS del documento aquí, así que una cita anidada
        // (Blockquote dentro de otro Blockquote) nunca se cuenta aparte.
        // La profundidad de cada línea se calcula al pintarla (ver
        // quoteDecorations), no aquí.
        blocks.push({ from: node.from, to: node.to, type: "quote" });
        break;
      default:
        // HTMLBlock, Table, LinkReference... siguen siendo
        // seleccionables/borrables, solo no convertibles.
        blocks.push({ from: node.from, to: node.to, type: "other" });
    }
  }
  blocks.sort((a, b) => a.from - b.from);
  return blocks;
}
