using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.FindSymbols;
using Microsoft.CodeAnalysis.Text;
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;

// Define the data structures for our JSON output
public class CodeGraph
{
    // Use Dictionary for unique IDs, then convert to List at the end
    [System.Text.Json.Serialization.JsonIgnore]
    public Dictionary<string, CodeNode> NodeMap { get; set; } = new Dictionary<string, CodeNode>();

    public List<CodeNode> Nodes => NodeMap.Values.ToList();
    public List<CodeEdge> Edges { get; set; } = new List<CodeEdge>();
}

public class CodeNode
{
    public string Id { get; set; }
    public string Type { get; set; }
    public string FilePath { get; set; }
    public int LineStart { get; set; }
    public int LineEnd { get; set; }
    public string Content { get; set; }
}

public class CodeEdge
{
    public string SourceId { get; set; }
    public string TargetId { get; set; }
    public string Type { get; set; }
}

public class Program
{
    public static async Task Main(string[] args)
    {
        // --- CLI Argument Parsing ---
        string projectPath = null;
        string outputPath = "code_graph.json";

        for (int i = 0; i < args.Length; i++)
        {
            if ((args[i] == "--project-path" || args[i] == "-p") && i + 1 < args.Length)
            {
                projectPath = args[++i];
            }
            else if ((args[i] == "--output" || args[i] == "-o") && i + 1 < args.Length)
            {
                outputPath = args[++i];
            }
            else if (args[i] == "--help" || args[i] == "-h")
            {
                Console.WriteLine("RoslynCodeGraph - Builds a code graph from decompiled C# source files.");
                Console.WriteLine();
                Console.WriteLine("Usage: RoslynCodeGraph --project-path <path> [--output <path>]");
                Console.WriteLine();
                Console.WriteLine("  --project-path, -p  (Required) Path to the folder containing .cs files.");
                Console.WriteLine("  --output, -o        (Optional) Output path for code_graph.json. Default: ./code_graph.json");
                Console.WriteLine("  --help, -h          Show this help message.");
                return;
            }
        }

        if (string.IsNullOrEmpty(projectPath))
        {
            Console.Error.WriteLine("Error: --project-path is required.");
            Console.Error.WriteLine("Usage: RoslynCodeGraph --project-path <path> [--output <path>]");
            Console.Error.WriteLine("Run with --help for more info.");
            Environment.ExitCode = 1;
            return;
        }

        if (!Directory.Exists(projectPath))
        {
            Console.Error.WriteLine($"Error: Project path does not exist: {projectPath}");
            Environment.ExitCode = 1;
            return;
        }

        Console.WriteLine($"Scanning project: {projectPath}");

        var graph = new CodeGraph();
        var workspace = new AdhocWorkspace();
        var solution = workspace.CurrentSolution;
        var project = solution.AddProject("MyGame", "MyGame", LanguageNames.CSharp);

        // Load all .cs files
        var csFiles = Directory.GetFiles(projectPath, "*.cs", SearchOption.AllDirectories);
        foreach (var file in csFiles)
        {
            var sourceText = SourceText.From(await File.ReadAllTextAsync(file));
            project = project.AddDocument(Path.GetFileName(file), sourceText,
                folders: new[] { Path.GetDirectoryName(file).Replace(projectPath, "").TrimStart(Path.DirectorySeparatorChar) })
                .Project;
        }

        var compilation = await project.GetCompilationAsync();
        if (compilation == null)
        {
            Console.Error.WriteLine("Failed to create compilation.");
            return;
        }

        Console.WriteLine($"Found {compilation.SyntaxTrees.Count()} syntax trees.");

        // --- Phase 1: Build all Nodes (Classes and Methods) ---
        foreach (var tree in compilation.SyntaxTrees)
        {
            var semanticModel = compilation.GetSemanticModel(tree);
            var root = await tree.GetRootAsync();
            var relativePath = tree.FilePath.Replace(projectPath, "").TrimStart(Path.DirectorySeparatorChar);

            // Find all classes
            foreach (var classDecl in root.DescendantNodes().OfType<ClassDeclarationSyntax>())
            {
                var symbol = semanticModel.GetDeclaredSymbol(classDecl);
                if (symbol == null) continue;

                string classId = symbol.ToDisplayString();
                // FIX: Use TryAdd to prevent duplicate IDs
                graph.NodeMap.TryAdd(classId, new CodeNode
                {
                    Id = classId,
                    Type = "class",
                    FilePath = relativePath,
                    LineStart = classDecl.GetLocation().GetLineSpan().StartLinePosition.Line,
                    LineEnd = classDecl.GetLocation().GetLineSpan().EndLinePosition.Line,
                    Content = classDecl.ToString()
                });

                // Find all methods within this class
                foreach (var methodDecl in classDecl.DescendantNodes().OfType<MethodDeclarationSyntax>())
                {
                    var methodSymbol = semanticModel.GetDeclaredSymbol(methodDecl);
                    if (methodSymbol == null) continue;

                    string methodId = methodSymbol.ToDisplayString();
                    // FIX: Use TryAdd to prevent duplicate IDs
                    graph.NodeMap.TryAdd(methodId, new CodeNode
                    {
                        Id = methodId,
                        Type = "method",
                        FilePath = relativePath,
                        LineStart = methodDecl.GetLocation().GetLineSpan().StartLinePosition.Line,
                        LineEnd = methodDecl.GetLocation().GetLineSpan().EndLinePosition.Line,
                        Content = methodDecl.ToString()
                    });
                }
            }
        }
        Console.WriteLine($"Identified {graph.Nodes.Count} nodes (classes and methods).");

        // --- Phase 2: Build all Edges (Method Calls) ---
        // FIX: This logic is rewritten to be more robust.
        foreach (var tree in compilation.SyntaxTrees)
        {
            var semanticModel = compilation.GetSemanticModel(tree);
            var root = await tree.GetRootAsync();

            // Find the method *declaration* that this syntax tree belongs to
            var methodDeclarations = root.DescendantNodes().OfType<MethodDeclarationSyntax>();

            foreach (var methodDecl in methodDeclarations)
            {
                var methodSymbol = semanticModel.GetDeclaredSymbol(methodDecl);
                if (methodSymbol == null) continue;
                string sourceId = methodSymbol.ToDisplayString();

                // Now find all *invocations* (calls) inside this method
                var invocations = methodDecl.DescendantNodes().OfType<InvocationExpressionSyntax>();

                foreach (var invocation in invocations)
                {
                    var symbolInfo = semanticModel.GetSymbolInfo(invocation);
                    var calledSymbol = symbolInfo.Symbol as IMethodSymbol;

                    if (calledSymbol != null)
                    {
                        string targetId = calledSymbol.OriginalDefinition.ToDisplayString();

                        // Ensure the target is one of the nodes we're tracking
                        if (graph.NodeMap.ContainsKey(targetId))
                        {
                            graph.Edges.Add(new CodeEdge
                            {
                                SourceId = sourceId,
                                TargetId = targetId,
                                Type = "calls"
                            });
                        }
                    }
                }
            }
        }
        Console.WriteLine($"Identified {graph.Edges.Count} call graph edges.");

        // --- Phase 3: Serialize and Save ---
        var options = new JsonSerializerOptions { WriteIndented = true };
        string json = JsonSerializer.Serialize(graph, options);

        // Ensure output directory exists
        string outputDir = Path.GetDirectoryName(Path.GetFullPath(outputPath));
        if (!string.IsNullOrEmpty(outputDir))
            Directory.CreateDirectory(outputDir);

        await File.WriteAllTextAsync(outputPath, json);

        Console.WriteLine($"Successfully created '{outputPath}'.");
    }
}