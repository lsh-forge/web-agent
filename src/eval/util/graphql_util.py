from typing import Any

from graphql import parse, DocumentNode
from graphql.language.ast import VariableDefinitionNode, FieldNode


def parse_graphql_query(query_str: str) -> dict[str, None | str | list[Any] | Any] | None:
    """使用graphql-core解析（更精准，支持复杂语法）"""
    try:
        # 解析AST抽象语法树
        doc: DocumentNode = parse(query_str)
        operation = doc.definitions[0]

        # 操作名
        operation_name = operation.name.value if operation.name else None

        # 查询目标（第一个字段）
        query_target = None
        if operation.selection_set.selections:
            field: FieldNode = operation.selection_set.selections[0]
            query_target = field.name.value

        # 参数
        parameters = []
        for var_def in operation.variable_definitions or []:
            var: VariableDefinitionNode = var_def
            param_name = var.variable.name.value
            param_type = var.type.name.value if hasattr(var.type, 'name') else str(var.type)
            if " at " in param_type:
                param_type = param_type.split(" at ")[0]
            is_required = '!' in str(var.type)  # 判断非空约束
            parameters.append({
                "param_name": param_name,
                "param_type": param_type,
                "is_required": is_required
            })

        return {
            "operation_name": operation_name,
            "query_target": query_target,
            "parameters": parameters
        }
    except Exception as e:
        return None


