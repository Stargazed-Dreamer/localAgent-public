# Refactor Candidates

TDD cycle 之后，寻找：

- **Duplication** -> Extract function/class
- **Long methods** -> Break into private helpers（tests 仍放在 public interface 上）
- **Shallow modules** -> Combine or deepen
- **Feature envy** -> Move logic to where data lives
- **Primitive obsession** -> Introduce value objects
- 新代码暴露为 problematic 的 **existing code**
