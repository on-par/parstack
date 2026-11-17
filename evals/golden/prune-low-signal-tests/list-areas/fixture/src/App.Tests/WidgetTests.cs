using Xunit;

public class WidgetTests
{
    [Fact]
    public void NameIsTrimmed()
    {
        Assert.Equal("w", new Widget(" w ").Name);
    }
}
