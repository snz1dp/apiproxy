
## 行为规范

- 无论我用什么语言提问，你必须用中文回答
- 不要跟我客套，不要说’好的，这是您的代码’，直接给代码！

## 运行环境

1. 本项目优先使用`conda`命令切换Python环境，禁止直接使用`python`命令切换环境。
2. 默认情况下使用`conda activate apiproxy`命令激活`apiproxy`环境，禁止使用其他环境名称。

## 代码规范

1. **Python版本**：本项目强制使用 Python 3.12，禁止使用老旧的 Python 版本。
2. **框架**：后端统一使用 FastAPI 框架进行开发，禁止使用其他后端框架。
3. **路径处理**：禁止使用老旧的 os.path 模块进行路径处理，统一使用 pathlib 模块。
4. **变量命名**：变量名必须清晰，禁止使用 x, y, temp 这种无意义命名。
5. **函数命名**：函数名应该使用小写字母和下划线分隔，禁止使用驼峰命名法。
6. **类命名**：类名应该使用驼峰命名法，禁止使用下划线分隔。
7. **注释**：代码中必须有必要的注释，特别是复杂的逻辑部分，禁止完全没有注释的代码。
8. **文档字符串**：每个函数和类必须有文档字符串，说明其功能、参数和返回值，禁止没有文档字符串的函数和类。
9. **代码格式**：遵循 PEP 8 代码风格指南，使用自动化工具如 Black 进行代码格式化，禁止不规范的代码格式。
10. **错误处理**：必须使用适当的异常处理机制，禁止使用过于宽泛的异常捕获，如 except Exception。
11. **依赖管理**：使用 uv 进行依赖管理，禁止直接使用 pip 安装依赖包。
12. **测试**：必须编写单元测试和集成测试，禁止没有测试覆盖的代码。

## 目录结构说明

**backend目录结构：**

- src/apiproxy OpenAI兼容代理服务根
- src/apiproxy/openaiproxy OpenAI兼容代理服务模块，包含核心业务逻辑和API接口实现
- src/apiproxy/openaiproxy/api OpenAI兼容代理服务管理API模块，包含API接口定义和实现代码
- src/apiproxy/openaiproxy/api/v1 OpenAI兼容代理服务API v1模块的代理实现
- src/apiproxy/openaiproxy/services OpenAI兼容代理服务业务逻辑模块，包含核心业务逻辑实现代码
- src/apiproxy/tests OpenAI兼容代理服务测试模块，包含单元测试和集成测试代码

## 数据库处理

- 数据库对象：使用 SQLAlchemy 定义数据库模型，禁止直接使用原始 SQL 语句进行数据库操作。
- 对象模型位置：所有数据库模型必须定义在 `src/apiproxy/openaiproxy/services/database/models/` 目录下，禁止在其他目录定义数据库模型。
- 对象模型定义：模型目录中使用`model.py`、`crud.py`、`utils.py`等文件进行数据库模型定义、CRUD操作和工具函数的实现，禁止在其他文件中定义数据库模型。
- 数据库迁移：使用 Alembic 进行数据库迁移管理，禁止手动修改数据库结构或直接执行 SQL 迁移脚本。
- 数据库连接：使用 SQLAlchemy 的连接池进行数据库连接管理，禁止直接使用数据库连接字符串进行连接。
- 数据库操作：所有数据库操作必须通过 SQLAlchemy 的 ORM 进行，禁止直接执行原始 SQL 语句进行数据操作。
- 数据库事务：必须使用 SQLAlchemy 的事务管理机制进行数据库操作，禁止手动管理数据库事务。
- 数据库异常处理：必须使用适当的异常处理机制处理数据库操作中的异常，禁止使用过于宽泛的异常捕获，如 except Exception。
- 数据库结构升级：
  在项目根目录使用 `make alembic-revision` 命令生成数据库迁移脚本，
  然后获取到最新的修订号`revision`，且同步修改
    `src/apiproxy/openaiproxy/services/database/service.py`
  文件中的`last_version = "xxxx"`字段为最新的修订号。
